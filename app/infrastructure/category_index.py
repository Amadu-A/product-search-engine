# app/infrastructure/category_index.py
"""Восстанавливаемая Qdrant-проекция PostgreSQL с outbox, hash reconciliation и RRF."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from app.domain.catalog import DependencyUnavailable, fingerprint, json_value, query_attributes


class CategoryIndex:
    """Перестраивает только собственные коллекции; сохраняет предыдущий alias target."""

    def __init__(self, store, inference, settings):
        """Получает SQL truth, embedding и проверяемую версию шаблона."""
        self.store, self.inference, self.settings = store, inference, settings
        self.contract = json.loads(
            Path("docs/embedding/category_index_v1.json").read_text(encoding="utf-8")
        )
        self.client = httpx.Client(
            base_url=settings.qdrant_url.rstrip("/"),
            timeout=settings.qdrant_timeout_seconds,
            headers={"api-key": settings.qdrant_api_key.get_secret_value()}
            if settings.qdrant_api_key.get_secret_value()
            else {},
        )
        self.alias = settings.qdrant_collection_alias
        self.event_ids = []
        if not re.fullmatch(r"product_taxonomy_categories_[a-zA-Z0-9_-]+", self.alias):
            raise ValueError(
                "Qdrant alias должен принадлежать namespace product_taxonomy_categories_"
            )

    def request(self, method: str, path: str, payload=None, missing=False):
        """Ограничивает HTTP; недоступность не маскируется состоянием ready."""
        try:
            response = self.client.request(method, path, json=payload)
            if missing and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json().get("result")
        except (httpx.HTTPError, ValueError) as error:
            raise DependencyUnavailable("Qdrant недоступен или отклонил контракт") from error

    def projection(self, category: dict, revision: int) -> tuple[str, dict]:
        """Сериализует template точно; непроверенные описания не входят в embedding."""
        reviewed = category["review_status"] == "approved"
        blank = self.contract["category_document"]["missing_text_placeholder"]
        document = self.contract["category_document"]["template"].format(
            category_name=category["name"],
            full_path=" > ".join(category["path_names"]),
            approved_aliases=", ".join(category["aliases"]) or blank,
            reviewed_description=(category["semantic_description"] if reviewed else None) or blank,
            reviewed_purpose=(category["semantic_purpose"] if reviewed else None) or blank,
            reviewed_exclusions=(category["semantic_exclusions"] if reviewed else None) or blank,
        )
        payload = {
            key: category[key]
            for key in (
                "parent_id",
                "path_ids",
                "path_names",
                "depth",
                "name",
                "source",
                "is_active",
                "is_assignable",
                "review_status",
            )
        }
        payload.update(
            category_id=str(category["id"]),
            document_sha256=hashlib.sha256(document.encode()).hexdigest(),
            taxonomy_revision=revision,
            aliases_version=fingerprint(category["aliases"]),
            document_version=self.contract["category_document"]["template_version"],
            embedding_model_revision=self.inference.embedding_revision,
            embedding_dimensions=self.inference.dimensions,
        )
        return document, json_value(payload)

    def scroll(self, collection: str) -> dict:
        """Сверяет реальные UUID/payload всех точек, не indexed_vectors_count."""
        points, offset = {}, None
        while True:
            payload = {"limit": 256, "with_payload": True, "with_vector": False}
            if offset is not None:
                payload["offset"] = offset
            result = self.request("POST", f"/collections/{collection}/points/scroll", payload)
            for point in result["points"]:
                points[str(point["id"])] = point["payload"]
            offset = result.get("next_page_offset")
            if offset is None:
                return points

    def status(self) -> dict:
        """Возвращает durable index state и outbox backlog независимо от UI."""
        with self.store.transaction() as unit:
            states = unit.all("taxonomy_index_state")
            outbox = unit.table("taxonomy_index_outbox")
            pending = unit.connection.scalar(
                sa.select(sa.func.count())
                .select_from(outbox)
                .where(outbox.c.processed_at.is_(None))
            )
            retryable = unit.connection.scalar(
                sa.select(sa.func.count())
                .select_from(outbox)
                .where(
                    outbox.c.processed_at.is_(None),
                    outbox.c.attempts < self.settings.taxonomy_outbox_max_attempts,
                    sa.or_(
                        outbox.c.next_attempt_at.is_(None),
                        outbox.c.next_attempt_at <= sa.func.now(),
                    ),
                )
            )
        state = states[0] if states else {"status": "uninitialized"}
        return json_value(
            {
                **state,
                "pending_events": pending,
                "retryable_events": retryable,
                "mode": self.inference.mode,
            }
        )

    def sync(self, force: bool = False) -> dict:
        """Под advisory lock проверяет проекцию и ACK только после wait=true и scroll."""
        with self.store.engine.connect() as lock:
            acquired = lock.scalar(
                sa.text("SELECT pg_try_advisory_lock(hashtext('product_taxonomy_index_v1'))")
            )
            lock.commit()
            if not acquired:
                return {
                    "status": "syncing",
                    "reason": "Другой index worker выполняет синхронизацию",
                }
            try:
                return self._sync(force)
            except Exception as error:
                if self.event_ids:
                    with self.store.transaction() as unit:
                        table = unit.table("taxonomy_index_outbox")
                        unit.connection.execute(
                            table.update()
                            .where(table.c.id.in_(self.event_ids))
                            .values(
                                last_error=f"{type(error).__name__}: Qdrant ACK не получен",
                                next_attempt_at=datetime.now(UTC) + timedelta(seconds=30),
                            )
                        )
                self.save_state(
                    status="error",
                    last_error=f"{type(error).__name__}: синхронизация не подтверждена",
                )
                raise
            finally:
                lock.execute(
                    sa.text("SELECT pg_advisory_unlock(hashtext('product_taxonomy_index_v1'))")
                )
                lock.commit()

    def save_state(self, **values) -> None:
        """Сохраняет наблюдаемое состояние; готовность не выводится из count оптимизатора."""
        with self.store.transaction() as unit:
            table = unit.table("taxonomy_index_state")
            defaults = {
                "collection_alias": self.alias,
                "embedding_dimensions": self.inference.dimensions,
            }
            unit.connection.execute(
                insert(table)
                .values(id=1, **defaults, **values)
                .on_conflict_do_update(index_elements=[table.c.id], set_={**defaults, **values})
            )

    def _sync(self, force: bool) -> dict:
        """Делает bounded batch upsert/delete, версионирует несовместимые индексы."""
        self.event_ids = []
        revision, snapshot = self.store.index_snapshot()
        with self.store.transaction() as unit:
            outbox = unit.table("taxonomy_index_outbox")
            pending = (
                unit.connection.execute(
                    sa.select(outbox)
                    .where(
                        outbox.c.processed_at.is_(None),
                        outbox.c.id <= revision,
                        outbox.c.attempts < self.settings.taxonomy_outbox_max_attempts,
                        sa.or_(
                            outbox.c.next_attempt_at.is_(None),
                            outbox.c.next_attempt_at <= sa.func.now(),
                        ),
                    )
                    .order_by(outbox.c.id)
                    .with_for_update(skip_locked=True)
                    .limit(10000)
                )
                .mappings()
                .all()
            )
            event_ids = [row["id"] for row in pending]
            self.event_ids = event_ids
            if event_ids:
                unit.connection.execute(
                    outbox.update()
                    .where(outbox.c.id.in_(event_ids))
                    .values(attempts=outbox.c.attempts + 1)
                )
        self.inference.ensure_revision(embedding=True)
        categories = [row for row in snapshot if row["is_active"] and row["ancestors_active"]]
        projections = {str(row["id"]): self.projection(row, revision) for row in categories}
        aliases = self.request("GET", "/aliases")["aliases"]
        current = next(
            (a["collection_name"] for a in aliases if a["alias_name"] == self.alias), None
        )
        version = fingerprint(
            {
                "revision": self.inference.embedding_revision,
                "dimensions": self.inference.dimensions,
                "template": self.contract["category_document"],
            }
        )[:12]
        prefix = "product_taxonomy_categories_v1_" + version + "_"
        rebuild = force or current is None or not current.startswith(prefix)
        target = prefix + uuid4().hex[:12] if rebuild else current
        self.save_state(status="syncing", last_error=None)
        if rebuild:
            self.request(
                "PUT",
                f"/collections/{target}",
                {"vectors": {"dense": {"size": self.inference.dimensions, "distance": "Cosine"}}},
            )
            actual = {}
        else:
            actual = self.scroll(target)
        changed = [
            key
            for key, (_, payload) in projections.items()
            if key not in actual
            or any(
                actual[key].get(field) != payload[field]
                for field in payload
                if field != "taxonomy_revision"
            )
        ]
        batch_size = self.settings.embedding_batch_size
        for start in range(0, len(changed), batch_size):
            keys = changed[start : start + batch_size]
            vectors = self.inference.embed([projections[key][0] for key in keys])
            points = [
                {"id": key, "vector": {"dense": vector}, "payload": projections[key][1]}
                for key, vector in zip(keys, vectors, strict=True)
            ]
            self.request("PUT", f"/collections/{target}/points?wait=true", {"points": points})
        deleted = sorted(set(actual) - set(projections))
        if deleted:
            self.request(
                "POST", f"/collections/{target}/points/delete?wait=true", {"points": deleted}
            )
        verified = self.scroll(target)
        if set(verified) != set(projections) or any(
            any(
                verified[key].get(field) != payload[field]
                for field in payload
                if field != "taxonomy_revision"
            )
            for key, (_, payload) in projections.items()
        ):
            raise DependencyUnavailable("Qdrant reconciliation: UUID/hash mismatch")
        if rebuild:
            actions = [{"delete_alias": {"alias_name": self.alias}}] if current else []
            actions.append({"create_alias": {"collection_name": target, "alias_name": self.alias}})
            self.request("POST", "/collections/aliases", {"actions": actions})
        now = datetime.now(UTC)
        with self.store.transaction() as unit:
            table = unit.table("taxonomy_index_outbox")
            unit.connection.execute(
                table.update()
                .where(table.c.id <= revision, table.c.processed_at.is_(None))
                .values(processed_at=now, last_error=None, next_attempt_at=None)
            )
        pending_now = self.status()["pending_events"]
        self.save_state(
            status="stale" if pending_now else "ready",
            collection_name=target,
            embedding_model_revision=self.inference.embedding_revision,
            document_version=self.contract["category_document"]["template_version"],
            taxonomy_fingerprint=fingerprint(
                {key: value[1]["document_sha256"] for key, value in projections.items()}
            ),
            last_successful_reconciliation=now,
            last_indexed_at=now,
            last_error=None,
        )
        return {
            **self.status(),
            "points": len(verified),
            "upserted": len(changed),
            "deleted": len(deleted),
            "previous_collection": current,
        }

    def candidates(self, understanding: dict) -> tuple[list[dict], list[str]]:
        """RRF dense/lexical с разрешёнными предками и обязательной SQL-проверкой UUID."""
        rows = [
            r
            for r in self.store.categories()
            if r["is_active"]
            and r["ancestors_active"]
            and r["is_assignable"]
            and r["review_status"] != "deprecated"
        ]
        by_id = {str(r["id"]): r for r in rows}
        tokens = set(
            re.findall(
                r"\w+",
                " ".join(
                    str(understanding.get(k) or "")
                    for k in ("product_type", "generic_type", "purpose")
                ).casefold(),
            )
        )
        lexical = [key for key in self.store.lexical_categories(tokens) if key in by_id]
        warnings, dense = [], []
        try:
            state = self.status()
            if state["status"] != "ready" or state["pending_events"]:
                raise DependencyUnavailable("Индекс отстаёт")
            blank = "не указано"
            query = self.contract["query_document"]["template"].format(
                product_type=understanding.get("product_type") or blank,
                generic_type=understanding.get("generic_type") or blank,
                purpose=understanding.get("purpose") or blank,
                applications=", ".join(understanding.get("applications", [])) or blank,
                type_disambiguating_attributes=query_attributes(understanding) or blank,
            )
            result = self.request(
                "POST",
                f"/collections/{self.alias}/points/query",
                {
                    "query": self.inference.embed([query])[0],
                    "using": "dense",
                    "limit": 20,
                    "with_payload": True,
                },
            )
            dense = [str(p["id"]) for p in result["points"] if str(p["id"]) in by_id]
        except DependencyUnavailable:
            warnings.append("vector_unavailable_or_stale")
        scores = {}
        for ranking in (dense, lexical):
            for rank, key in enumerate(ranking, 1):
                scores[key] = scores.get(key, 0) + 1 / (60 + rank)
        ordered = sorted(scores, key=lambda key: (-scores[key], key))
        selected = ordered[: max(1, self.settings.category_candidate_top_k - 2)]
        for key in list(selected):
            for ancestor in reversed(by_id[key]["path_ids"][:-1]):
                if ancestor in by_id and ancestor not in selected:
                    selected.append(ancestor)
        selected.extend(key for key in ordered if key not in selected)
        output = []
        for key in selected[: self.settings.category_candidate_top_k]:
            row = by_id[key]
            reviewed = row["review_status"] == "approved"
            output.append(
                {
                    "category_id": key,
                    **{
                        k: row[k]
                        for k in (
                            "name",
                            "path_ids",
                            "path_names",
                            "is_active",
                            "is_assignable",
                            "review_status",
                        )
                    },
                    "description": row["semantic_description"] if reviewed else None,
                    "purpose": row["semantic_purpose"] if reviewed else None,
                    "exclusions": row["semantic_exclusions"] if reviewed else None,
                    "score": scores.get(key, 0),
                    "retrieval_method": "lexical" if warnings else "rrf_dense_lexical",
                }
            )
        return output, warnings
