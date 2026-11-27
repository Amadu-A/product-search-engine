# app/infrastructure/catalog_store.py
"""Транзакционный SQL-адаптер импорта; таблицы отражаются из применённых миграций."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import distinct_on, insert

from app.domain.catalog import CatalogError, ConflictError, fingerprint, json_value
from app.domain.product_identity import (
    IDENTITY_LIMIT,
    accumulate_profile,
    canonical_brand,
    claim_value,
    decide_identity,
    identity_conflicts,
    identity_profile,
    identity_retrieval_keys,
    refine_legacy_roles,
    searchable_mpn,
    text_key,
)


class CatalogStore:
    """Предоставляет небольшой UoW; SQL и блокировки остаются в infrastructure."""

    def __init__(self, engine):
        """Проверяет миграцию отражением общей схемы без create_all."""
        self.engine = engine
        self.metadata = sa.MetaData()
        self.metadata.reflect(engine)
        self.tables = self.metadata.tables

    @contextmanager
    def transaction(self) -> Iterator["CatalogSession"]:
        """Коммитит все изменения либо откатывает raw, результаты, счётчики и events вместе."""
        with self.engine.begin() as connection:
            yield CatalogSession(connection, self.tables)

    @contextmanager
    def readonly(self) -> Iterator["CatalogSession"]:
        """Читает один согласованный снимок, PostgreSQL запрещает запись в этой транзакции."""
        with self.engine.begin() as connection:
            connection.execute(
                sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            yield CatalogSession(connection, self.tables)

    def categories(self) -> list[dict]:
        """Читает актуальное дерево и только утверждённые алиасы."""
        with self.transaction() as unit:
            return self._category_rows(unit)

    def index_snapshot(self) -> tuple[int, list[dict]]:
        """Согласованно читает SQL-дерево и outbox revision до сетевых вызовов."""
        with self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            with connection.begin():
                unit = CatalogSession(connection, self.tables)
                return unit.taxonomy_revision(), self._category_rows(unit)

    def _category_rows(self, unit) -> list[dict]:
        """Строит одинаковую SQL-проекцию для API и согласованного index snapshot."""
        rows = unit.all("categories")
        aliases = unit.all("category_aliases", status="approved")
        products = unit.table("products")
        counts = dict(
            unit.connection.execute(
                sa.select(products.c.category_id, sa.func.count())
                .where(products.c.status != "retired")
                .group_by(products.c.category_id)
            ).all()
        )
        by_id = {str(row["id"]): row for row in rows}
        for row in rows:
            row["product_count"] = counts.get(row["id"], 0)
            row["subtree_product_count"] = 0
            path, current, seen = [], row, set()
            while current:
                key = str(current["id"])
                if key in seen:
                    raise CatalogError("Цикл в SQL-дереве")
                seen.add(key)
                path.insert(0, current)
                current = by_id.get(str(current["parent_id"])) if current["parent_id"] else None
            row["path_ids"] = [str(p["id"]) for p in path]
            row["path_names"] = [p["name"] for p in path]
            row["depth"] = len(path) - 1
            row["aliases"] = sorted(a["alias"] for a in aliases if a["category_id"] == row["id"])
            row["ancestors_active"] = all(p["is_active"] for p in path)
            row["ancestor_exclusions"] = [
                p["semantic_exclusions"]
                for p in path[:-1]
                if p["review_status"] == "approved" and p["semantic_exclusions"]
            ]
        for row in rows:
            if row["ancestors_active"] and row["review_status"] != "deprecated":
                for category_id in row["path_ids"]:
                    by_id[category_id]["subtree_product_count"] += row["product_count"]
        return rows

    def lexical_categories(self, tokens: set[str], limit: int = 20) -> list[str]:
        """Ранжирует в PostgreSQL имена, пути и approved aliases параметризованным запросом."""
        words = sorted(tokens)[:24]
        if not words:
            return []
        cases = [
            f"CASE WHEN lower(search_text) LIKE :word_{i} THEN 1 ELSE 0 END"
            for i in range(len(words))
        ]
        score = " + ".join(cases)
        query = sa.text(
            """
            WITH RECURSIVE tree AS (
              SELECT id,parent_id,name::text AS path FROM categories WHERE parent_id IS NULL
              UNION ALL
              SELECT c.id,c.parent_id,t.path || ' > ' || c.name
              FROM categories c JOIN tree t ON c.parent_id=t.id
            ), docs AS (
              SELECT t.id,t.path || ' ' || coalesce(string_agg(a.alias,' '),'') AS search_text
              FROM tree t LEFT JOIN category_aliases a ON a.category_id=t.id AND a.status='approved'
              GROUP BY t.id,t.path
            ) SELECT id FROM docs WHERE ("""
            + score
            + ")>0 ORDER BY ("
            + score
            + ") DESC,id LIMIT :limit"
        )
        params = {f"word_{i}": "%" + word.replace("_", r"\_") + "%" for i, word in enumerate(words)}
        with self.engine.connect() as connection:
            return [str(row[0]) for row in connection.execute(query, {**params, "limit": limit})]

    @contextmanager
    def record_lock(self, record_id):
        """Сериализует обработку одного normalized UUID между jobs без открытой SQL-транзакции."""
        with self.engine.connect() as connection:
            connection.execute(
                sa.text("SELECT pg_advisory_lock(hashtextextended(:key,0))"),
                {"key": str(record_id)},
            )
            connection.commit()
            try:
                yield
            finally:
                connection.execute(
                    sa.text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"),
                    {"key": str(record_id)},
                )
                connection.commit()

    def claim_job(self, job_id: str | None = None) -> str | None:
        """Берёт queued job под транзакционной блокировкой; lease допускает восстановление."""
        with self.transaction() as unit:
            table = unit.table("ingest_jobs")
            query = sa.select(table).where(table.c.status == "queued")
            if job_id:
                query = query.where(table.c.id == UUID(job_id))
            row = (
                unit.connection.execute(
                    query.order_by(table.c.created_at).with_for_update(skip_locked=True).limit(1)
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            unit.update(
                "ingest_jobs",
                row["id"],
                status="running",
                started_at=row["started_at"] or datetime.now(UTC),
            )
            unit.event(row["id"], "job.started", "Задание принято worker", stage="normalizing")
            return str(row["id"])

    def recover(self, lease_seconds: int) -> int:
        """Возвращает потерянные running jobs в очередь, сохраняя terminal items."""
        with self.transaction() as unit:
            table = unit.table("ingest_jobs")
            result = unit.connection.execute(
                table.update()
                .where(
                    table.c.status.in_(["running", "cancel_requested"]),
                    table.c.updated_at
                    < sa.func.now() - sa.func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds),
                )
                .values(status="queued", updated_at=sa.func.now())
            )
            return result.rowcount


class CatalogSession:
    """Операции одной транзакции; входные имена таблиц задаются только кодом приложения."""

    def __init__(self, connection, tables):
        """Сохраняет connection и frozen schema adapter."""
        self.connection, self.tables = connection, tables

    def table(self, name: str):
        """Возвращает отражённый Table по внутреннему имени."""
        return self.tables[name]

    def expired_uploads(self, ttl_seconds: int) -> list[dict]:
        """Возвращает только завершённые/остановленные upload-файлы старше TTL."""
        files, jobs = self.table("ingest_job_files"), self.table("ingest_jobs")
        query = (
            sa.select(files)
            .join(jobs, jobs.c.id == files.c.job_id)
            .where(
                files.c.created_at < datetime.now(UTC) - timedelta(seconds=ttl_seconds),
                jobs.c.status.in_(["completed", "completed_with_issues", "failed", "canceled"]),
            )
        )
        return [dict(row) for row in self.connection.execute(query).mappings()]

    def all(self, name: str, /, **filters) -> list[dict]:
        """Выбирает строки по параметризованному равенству."""
        table = self.table(name)
        query = sa.select(table).filter_by(**filters)
        return [dict(row) for row in self.connection.execute(query).mappings()]

    def get(self, name: str, row_id, lock: bool = False) -> dict:
        """Читает существующий UUID, при необходимости FOR UPDATE."""
        table = self.table(name)
        query = sa.select(table).where(table.c.id == UUID(str(row_id)))
        if lock:
            query = query.with_for_update()
        row = self.connection.execute(query).mappings().first()
        if row is None:
            raise CatalogError(f"Запись {name} не найдена")
        return dict(row)

    def add(self, name: str, /, **values) -> dict:
        """Вставляет UUID запись и возвращает server defaults."""
        values.setdefault("id", uuid4())
        return dict(
            self.connection.execute(
                self.table(name).insert().values(**values).returning(self.table(name))
            )
            .mappings()
            .one()
        )

    def add_items(self, rows: list[dict]) -> None:
        """Сохраняет ограниченную пачку строк и даёт триггеру применить один delta."""
        if rows:
            self.connection.execute(self.table("ingest_job_items").insert().values(rows))

    def update(self, name: str, row_id, /, **values) -> None:
        """Изменяет только указанные поля, оставляя ручные данные вне update."""
        table = self.table(name)
        if "updated_at" in table.c:
            values["updated_at"] = datetime.now(UTC)
        self.connection.execute(
            table.update().where(table.c.id == UUID(str(row_id))).values(**values)
        )

    def unique(self, name: str, keys: dict, /, **values) -> dict:
        """Идемпотентно вставляет по SQL unique без перезаписи ранее принятого решения."""
        table = self.table(name)
        inserted = (
            self.connection.execute(
                insert(table)
                .values(id=uuid4(), **keys, **values)
                .on_conflict_do_nothing()
                .returning(table)
            )
            .mappings()
            .one_or_none()
        )
        if inserted is not None:
            return dict(inserted)
        rows = self.all(name, **keys)
        if len(rows) != 1:
            raise ConflictError("Неоднозначная идентичность записи")
        return rows[0]

    def event(
        self, job_id, kind: str, message: str, *, item_id=None, stage=None, level="info", data=None
    ) -> None:
        """Записывает replayable event в той же транзакции, что изменение данных."""
        self.connection.execute(
            self.table("ingest_job_events")
            .insert()
            .values(
                job_id=job_id,
                item_id=item_id,
                type=kind,
                stage=stage,
                level=level,
                message=message,
                data=json_value(data or {}),
            )
        )

    def connection_insert_run(self, job_id, file_id, run_id) -> None:
        """Связывает multi-file job с отдельным SourceRun каждого файла."""
        self.connection.execute(
            self.table("ingest_job_source_runs")
            .insert()
            .values(job_id=job_id, file_id=file_id, source_run_id=run_id)
        )

    def save_understanding(self, normalized_id, provenance: dict, result: dict) -> None:
        """Заполняет cache; техническую ошибку допускает заменить успешным retry."""
        table = self.table("product_understandings")
        values = {
            "answer_json": result["answer_json"],
            "raw_response_text": result["raw_response_text"],
            "evidence_refs": result["answer_json"]["evidence_ids"],
            "status": result["answer_json"]["status"],
        }
        self.connection.execute(
            insert(table)
            .values(id=uuid4(), normalized_record_id=normalized_id, **provenance, **values)
            .on_conflict_do_update(
                index_elements=[
                    table.c.normalized_record_id,
                    table.c.input_sha256,
                    table.c.prompt_sha256,
                    table.c.model_logical_id,
                    table.c.model_revision,
                ],
                set_=values,
                where=table.c.status.in_(["invalid_output", "unavailable"]),
            )
        )

    def events(self, job_id, after: int, limit: int = 100) -> list[dict]:
        """Читает cursor только указанного job без пропуска событий после reconnect."""
        table = self.table("ingest_job_events")
        query = (
            sa.select(table)
            .where(table.c.job_id == UUID(str(job_id)), table.c.event_id > after)
            .order_by(table.c.event_id)
            .limit(limit)
        )
        return [dict(row) for row in self.connection.execute(query).mappings()]

    def command(self, actor: str, key: str, request: dict) -> dict | None:
        """Сериализует ключ команды и запрещает повтор ключа с другим содержимым."""
        self.connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": actor + ":" + key},
        )
        rows = self.all("ingest_commands", actor=actor, idempotency_key=key)
        if not rows:
            return None
        if rows[0]["request_sha256"] != fingerprint(request):
            raise ConflictError("Ключ идемпотентности уже использован для другой команды")
        return rows[0]["response"]

    def save_command(self, actor: str, key: str, request: dict, response: dict) -> None:
        """Сохраняет результат успешно принятой команды атомарно с её эффектом."""
        self.connection.execute(
            self.table("ingest_commands")
            .insert()
            .values(
                actor=actor,
                idempotency_key=key,
                request_sha256=fingerprint(request),
                response=json_value(response),
            )
        )

    def refresh_counts(self, job_id) -> dict:
        """Читает транзакционные счётчики задания, поддерживаемые PostgreSQL."""
        job = self.get("ingest_jobs", job_id)
        return {
            key: job[key]
            for key in (
                "total_count",
                "processed_count",
                "assigned_count",
                "needs_review_count",
                "taxonomy_gap_count",
                "error_count",
            )
        }

    def reclassification_items(self, job_id, only_remaining: bool = False) -> list[dict]:
        """Выбирает незакрытые решения и старые назначения без Product."""
        items, decisions = (self.table(n) for n in ("ingest_job_items", "classification_decisions"))
        related = decisions.c.normalized_record_id == items.c.normalized_record_id
        manual = sa.exists().where(
            related, decisions.c.method == "manual", decisions.c.status == "approved"
        )
        proposed = sa.exists().where(related, decisions.c.status == "proposed")
        if only_remaining:
            eligible = ~manual & (items.c.status == "needs_review")
        else:
            eligible = sa.or_(
                (items.c.status == "assigned") & items.c.product_id.is_(None),
                ~manual
                & (items.c.status != "not_product")
                & sa.or_(items.c.status.in_(["needs_review", "taxonomy_gap"]), proposed),
            )
        products = self.table("products")
        retired = sa.exists().where(
            products.c.id == items.c.product_id, products.c.status == "retired"
        )
        query = sa.select(items).where(
            items.c.job_id == UUID(str(job_id)),
            items.c.normalized_record_id.is_not(None),
            ~retired,
            eligible,
        )
        return [dict(row) for row in self.connection.execute(query).mappings()]

    def queue_reclassification(self, item: dict) -> bool:
        """Ставит в очередь лишь неизменившуюся позицию и инвалидирует старый UI preview."""
        table = self.table("ingest_job_items")
        result = self.connection.execute(
            table.update()
            .where(
                table.c.id == item["id"],
                table.c.processing_version == item["processing_version"],
                table.c.status == item["status"],
            )
            .values(
                status="pending",
                issue_codes=sorted(set(item["issue_codes"]) | {"reclassify_requested"}),
                processing_version=item["processing_version"] + 1,
                updated_at=datetime.now(UTC),
            )
        )
        return result.rowcount == 1

    def manual_category_exists(self, items):
        """Определяет ручное назначение по действующему решению и той же категории."""
        decisions = self.table("classification_decisions")
        return sa.exists().where(
            decisions.c.normalized_record_id == items.c.normalized_record_id,
            decisions.c.category_id == items.c.category_id,
            decisions.c.method == "manual",
            decisions.c.status == "approved",
        )

    def assignment_counts(self, job_id) -> dict[str, int]:
        """Считает ручные, автоматические и исключённые строки без изменения схемы."""
        items, products = (self.table(n) for n in ("ingest_job_items", "products"))
        manual = self.manual_category_exists(items)
        assigned = items.c.status == "assigned"
        archived = items.c.issue_codes.contains(["archived_not_product"])
        query = (
            sa.select(
                sa.func.count().filter(assigned & ~manual).label("auto_assigned_count"),
                sa.func.count().filter(assigned & manual).label("manual_assigned_count"),
                sa.func.count()
                .filter(assigned & items.c.product_id.is_(None))
                .label("assigned_without_product_count"),
                sa.func.count(sa.distinct(products.c.id))
                .filter(products.c.category_id.is_not(None) & (products.c.status != "retired"))
                .label("categorized_products_count"),
                sa.func.count()
                .filter((items.c.status == "not_product") & ~archived)
                .label("not_product_count"),
                sa.func.count().filter(archived).label("archived_not_product_count"),
                sa.func.count()
                .filter(
                    sa.or_(
                        items.c.status.in_(["error", "invalid_record"]),
                        (items.c.status == "needs_review")
                        & sa.or_(
                            *[
                                items.c.issue_codes.contains([code])
                                for code in (
                                    "classification_technical",
                                    "invalid_output",
                                    "inference_unavailable",
                                )
                            ]
                        ),
                    )
                )
                .label("technical_error_count"),
            )
            .select_from(items.outerjoin(products, items.c.product_id == products.c.id))
            .where(items.c.job_id == UUID(str(job_id)))
        )
        created = self.connection.scalar(
            sa.select(sa.func.count())
            .select_from(products)
            .where(products.c.attributes["created_in_job_id"].astext == str(job_id))
        )
        return {
            **self.connection.execute(query).mappings().one(),
            "products_created_count": created,
        }

    def item_details(self, item_id) -> dict:
        """Собирает trace raw → normalized → understanding → matching → classification."""
        item = self.get("ingest_job_items", item_id)
        result = {
            "item": item,
            "raw": None,
            "normalized": None,
            "understandings": [],
            "matches": [],
            "decisions": [],
            "observations": [],
            "product": self.get("products", item["product_id"]) if item["product_id"] else None,
        }
        if item["raw_record_id"]:
            result["raw"] = self.get("raw_records", item["raw_record_id"])
            result["observations"] = self.all(
                "price_observations", raw_record_id=item["raw_record_id"]
            )
        if item["normalized_record_id"]:
            key = item["normalized_record_id"]
            result["normalized"] = self.get("normalized_records", key)
            result["understandings"] = self.all("product_understandings", normalized_record_id=key)
            result["matches"] = self.all("product_match_decisions", normalized_record_id=key)
            result["decisions"] = self.all("classification_decisions", normalized_record_id=key)
        return result

    def items_page(
        self, job_id, status, query: str, offset: int, limit: int, include_archived: bool = False
    ) -> dict:
        """SQL join/pagination таблицы items без обхода каждого raw отдельным запросом."""
        items, norm, categories = (
            self.table(n) for n in ("ingest_job_items", "normalized_records", "categories")
        )
        condition = [items.c.job_id == UUID(str(job_id))]
        if not include_archived:
            condition.append(~items.c.issue_codes.contains(["archived_not_product"]))
        if status:
            condition.append(
                items.c.status.in_(["needs_review", "taxonomy_gap", "invalid_record", "error"])
                if status == "unresolved"
                else items.c.status == status
            )
        if query:
            condition.append(norm.c.normalized_name.icontains(query, autoescape=True))
        source = items.outerjoin(norm, norm.c.id == items.c.normalized_record_id).outerjoin(
            categories, categories.c.id == items.c.category_id
        )
        total = self.connection.scalar(
            sa.select(sa.func.count()).select_from(source).where(*condition)
        )
        statement = (
            sa.select(
                items,
                norm.c.normalized_name.label("name"),
                categories.c.name.label("category_name"),
                sa.case(
                    (items.c.status != "assigned", None),
                    (self.manual_category_exists(items), "manual"),
                    else_="auto",
                ).label("assignment_origin"),
            )
            .select_from(source)
            .where(*condition)
            .order_by(items.c.file_id, items.c.item_position)
            .offset(offset)
            .limit(limit)
        )
        return {
            "items": [dict(r) for r in self.connection.execute(statement).mappings()],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def request_reindex(self) -> None:
        """Записывает события для всех категорий; worker проверяет SQL snapshot заново."""
        table, categories = self.table("taxonomy_index_outbox"), self.table("categories")
        self.connection.execute(
            table.insert().from_select(
                ["entity_kind", "entity_id", "event_kind"],
                sa.select(sa.literal("category"), categories.c.id, sa.literal("upsert")),
            )
        )

    def products_query(self):
        """Обогащает Product названиями справочников одним SQL-запросом."""
        products, brands, manufacturers, categories = (
            self.table(n) for n in ("products", "brands", "manufacturers", "categories")
        )
        return (
            sa.select(
                products,
                brands.c.canonical_name.label("brand_name"),
                brands.c.status.label("brand_status"),
                manufacturers.c.legal_name.label("manufacturer_name"),
                manufacturers.c.status.label("manufacturer_status"),
                categories.c.name.label("category_name"),
            )
            .outerjoin(brands, brands.c.id == products.c.brand_id)
            .outerjoin(manufacturers, manufacturers.c.id == products.c.manufacturer_id)
            .outerjoin(categories, categories.c.id == products.c.category_id)
        )

    def products_page(
        self,
        query: str,
        offset: int,
        limit: int,
        category_id: UUID | None = None,
        uncategorized: bool = False,
        manufacturer_id: UUID | None = None,
        sort: str = "created_asc",
    ) -> dict:
        """Фильтрует по ветке и производителю, ищет имя или артикул и стабильно сортирует."""
        from app.domain.catalog import normalized_key

        table = self.table("products")
        conditions = [table.c.status != "retired"]
        if query := query.strip():
            search = [
                table.c.canonical_name.icontains(query, autoescape=True),
                table.c.mpn.icontains(query, autoescape=True),
            ]
            if key := normalized_key(query):
                search.append(table.c.normalized_mpn.contains(key, autoescape=True))
            conditions.append(sa.or_(*search))
        if uncategorized:
            conditions.append(table.c.category_id.is_(None))
        elif category_id:
            categories = self.table("categories")
            active = (categories.c.is_active, categories.c.review_status != "deprecated")
            branch = sa.select(categories.c.id).where(categories.c.id == category_id, *active)
            tree = branch.cte("catalog_branch", recursive=True)
            tree = tree.union(
                sa.select(categories.c.id)
                .join(tree, categories.c.parent_id == tree.c.id)
                .where(*active)
            )
            conditions.append(table.c.category_id.in_(sa.select(tree.c.id)))
        if manufacturer_id:
            conditions.append(table.c.manufacturer_id == manufacturer_id)
        order = {
            "created_asc": table.c.created_at.asc(),
            "created_desc": table.c.created_at.desc(),
            "name_asc": sa.func.lower(table.c.canonical_name).asc(),
            "name_desc": sa.func.lower(table.c.canonical_name).desc(),
            "mpn_asc": table.c.normalized_mpn.asc().nulls_last(),
        }[sort]
        count = self.connection.scalar(
            sa.select(sa.func.count()).select_from(table).where(*conditions)
        )
        rows = [
            dict(row)
            for row in self.connection.execute(
                self.products_query()
                .where(*conditions)
                .order_by(order, table.c.id)
                .offset(offset)
                .limit(limit)
            ).mappings()
        ]
        self.catalog_offer_summary(rows)
        return {
            "items": rows,
            "total": count,
            "offset": offset,
            "limit": limit,
        }

    def catalog_offer_summary(self, rows: list[dict]) -> None:
        """Суммирует продавцов и последнее сохранённое наблюдение только для страницы."""
        if not rows:
            return
        by_id = {row["id"]: row for row in rows}
        offers, obs, history = (
            self.table(n) for n in ("offers", "price_observations", "price_history")
        )
        condition = (offers.c.status != "retired") & offers.c.product_id.in_(by_id)
        sellers = (
            sa.select(
                offers.c.product_id,
                sa.func.count(sa.distinct(offers.c.seller_id)).label("quantity"),
            )
            .where(condition)
            .group_by(offers.c.product_id)
        )
        for row in self.connection.execute(sellers).mappings():
            by_id[row["product_id"]]["seller_count"] = row["quantity"]
        amount = sa.func.coalesce(history.c.amount, obs.c.amount)
        price = (
            sa.select(
                offers.c.product_id,
                amount.label("amount"),
                sa.func.coalesce(history.c.currency, obs.c.currency).label("currency"),
                obs.c.source_reported_date,
                obs.c.observed_at,
            )
            .join(obs, obs.c.offer_id == offers.c.id)
            .outerjoin(history, history.c.price_observation_id == obs.c.id)
            .where(
                condition, amount.is_not(None), self.observation_matches(obs, offers.c.product_id)
            )
            .ext(distinct_on(offers.c.product_id))
            .order_by(
                offers.c.product_id,
                obs.c.source_reported_date.desc().nulls_last(),
                obs.c.observed_at.desc(),
                obs.c.id,
            )
        )
        for row in self.connection.execute(price).mappings():
            by_id[row["product_id"]]["historical_price"] = dict(row)

    def observation_matches(self, observations, product_id):
        """Исключает цены исходников, привязанных к другому исполнению изделия."""
        items = self.table("ingest_job_items")
        links = sa.exists().where(items.c.raw_record_id == observations.c.raw_record_id)
        return sa.or_(
            ~links.where(items.c.product_id.is_not(None)),
            links.where(items.c.product_id == product_id),
        )

    def catalog_manufacturers(self) -> list[dict]:
        """Возвращает доступные варианты фильтра только для товаров каталога."""
        product, manufacturer = (self.table(n) for n in ("products", "manufacturers"))
        query = (
            sa.select(
                manufacturer.c.id, manufacturer.c.legal_name.label("name"), manufacturer.c.status
            )
            .join(product, product.c.manufacturer_id == manufacturer.c.id)
            .where(product.c.status != "retired")
            .distinct()
            .order_by(manufacturer.c.legal_name, manufacturer.c.id)
        )
        return [dict(row) for row in self.connection.execute(query).mappings()]

    def product_detail(self, product_id: UUID) -> dict | None:
        """Читает карточку действующего Product и ссылки исходных наблюдений."""
        products, categories, items, raw, sources = (
            self.table(n)
            for n in ("products", "categories", "ingest_job_items", "raw_records", "sources")
        )
        product_id = self.resolve_product_id(product_id)
        row = (
            self.connection.execute(
                self.products_query().where(
                    products.c.id == product_id, products.c.status != "retired"
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        path = []
        if row["category_id"]:
            parent = sa.select(categories.c.id, categories.c.parent_id, categories.c.name)
            tree = parent.where(categories.c.id == row["category_id"]).cte(
                "product_path", recursive=True
            )
            tree = tree.union(parent.join(tree, categories.c.id == tree.c.parent_id))
            nodes = {c["id"]: c for c in self.connection.execute(sa.select(tree)).mappings()}
            current = row["category_id"]
            while current in nodes and len(path) < len(nodes):
                node = nodes[current]
                path.append(node["name"])
                current = node["parent_id"]
            path.reverse()
        links = (
            sa.select(raw.c.origin_url.label("url"), sources.c.name.label("source_name"))
            .select_from(
                items.join(raw, items.c.raw_record_id == raw.c.id).join(
                    sources, sources.c.id == raw.c.source_id
                )
            )
            .where(items.c.product_id == product_id, raw.c.origin_url.is_not(None))
            .distinct()
            .order_by(sources.c.name, raw.c.origin_url)
        )
        return {
            **row,
            "category_path": path,
            "source_links": [dict(link) for link in self.connection.execute(links).mappings()],
        }

    def product_offers(
        self, product_id: UUID, offset: int, limit: int, history_offset: int, history_limit: int
    ) -> dict | None:
        """Читает предложения с продавцами и ограниченной историей за два набора строк."""
        products, offers, sellers, sources = (
            self.table(n) for n in ("products", "offers", "sellers", "sources")
        )
        product_id = self.resolve_product_id(product_id)
        if not self.connection.scalar(
            sa.select(products.c.id).where(
                products.c.id == product_id, products.c.status != "retired"
            )
        ):
            return None
        condition = (offers.c.product_id == product_id) & (offers.c.status != "retired")
        total = self.connection.scalar(
            sa.select(sa.func.count()).select_from(offers).where(condition)
        )
        query = (
            sa.select(
                offers,
                sellers.c.legal_name.label("seller_name"),
                sources.c.name.label("source_name"),
            )
            .outerjoin(sellers, sellers.c.id == offers.c.seller_id)
            .join(sources, sources.c.id == offers.c.source_id)
            .where(condition)
            .order_by(offers.c.created_at, offers.c.id)
            .offset(offset)
            .limit(limit)
        )
        rows = [
            dict(row, price_history=[], history_total=0)
            for row in self.connection.execute(query).mappings()
        ]
        self.populate_offer_titles(product_id, rows)
        self.populate_price_history(product_id, rows, history_offset, history_limit)
        return {"items": rows, "total": total, "offset": offset, "limit": limit}

    def resolve_product_id(self, identifier: UUID) -> UUID:
        """Читает перенаправление старого UUID, оставленного после доказанного объединения."""
        aliases = self.table("product_merge_redirects")
        return (
            self.connection.scalar(
                sa.select(aliases.c.product_id).where(aliases.c.old_product_id == identifier)
            )
            or identifier
        )

    def populate_offer_titles(self, product_id: UUID, offers: list[dict]) -> None:
        """Показывает название и ссылку исходной строки без замены на Product.name."""
        if not offers:
            return
        by_offer = {row["id"]: row for row in offers}
        obs, raw, norm = (
            self.table(n) for n in ("price_observations", "raw_records", "normalized_records")
        )
        title = (
            sa.select(norm.c.attributes["raw_name"].astext)
            .where(norm.c.raw_record_id == raw.c.id)
            .order_by(norm.c.created_at.desc(), norm.c.id)
            .limit(1)
            .scalar_subquery()
        )
        query = (
            sa.select(
                obs.c.offer_id,
                sa.func.coalesce(title, raw.c.payload["name"].astext).label("original_name"),
                raw.c.origin_url,
            )
            .join(raw, raw.c.id == obs.c.raw_record_id)
            .where(obs.c.offer_id.in_(by_offer), self.observation_matches(obs, product_id))
            .ext(distinct_on(obs.c.offer_id))
            .order_by(
                obs.c.offer_id,
                obs.c.source_reported_date.desc().nulls_last(),
                obs.c.observed_at.desc(),
                obs.c.id,
            )
        )
        for entry in self.connection.execute(query).mappings():
            row = by_offer[entry["offer_id"]]
            row["original_name"] = entry["original_name"]
            row["origin_url"] = entry["origin_url"] or row["origin_url"]

    def populate_price_history(
        self, product_id: UUID, offers: list[dict], offset: int, limit: int
    ) -> None:
        """Возвращает исходные наблюдения и сведения о верификации с сохранением дат."""
        if not offers:
            return
        obs, history = (self.table(n) for n in ("price_observations", "price_history"))
        by_offer = {row["id"]: row for row in offers}
        order = (obs.c.source_reported_date.desc().nulls_last(), obs.c.observed_at.desc(), obs.c.id)
        window = (
            sa.select(
                obs,
                history.c.valid_from,
                history.c.valid_to,
                history.c.amount.label("verified_amount"),
                history.c.currency.label("verified_currency"),
                history.c.sale_unit.label("verified_unit"),
                sa.func.count().over(partition_by=obs.c.offer_id).label("history_count"),
                sa.func.row_number()
                .over(partition_by=obs.c.offer_id, order_by=order)
                .label("rank"),
            )
            .outerjoin(history, history.c.price_observation_id == obs.c.id)
            .where(
                obs.c.offer_id.in_(by_offer),
                self.observation_matches(obs, product_id),
                sa.or_(
                    obs.c.raw_price.is_not(None),
                    obs.c.amount.is_not(None),
                    history.c.id.is_not(None),
                ),
            )
            .subquery()
        )
        within_page = (window.c.rank > offset) & (window.c.rank <= offset + limit)
        query = (
            sa.select(window)
            .where(sa.or_(within_page, window.c.rank == 1))
            .order_by(window.c.offer_id, window.c.rank)
        )
        for point in self.connection.execute(query).mappings():
            target = by_offer[point["offer_id"]]
            target["history_total"] = point["history_count"]
            if point["rank"] > offset:
                target["price_history"].append(dict(point))

    def taxonomy_revision(self) -> int:
        """Возвращает монотонную revision SQL-изменений категорий и approved aliases."""
        table = self.table("taxonomy_index_outbox")
        return self.connection.scalar(sa.select(sa.func.coalesce(sa.func.max(table.c.id), 0)))

    def catalog_counts(self) -> dict:
        """Считает физические строки всей БД, явно отдельно от counters одного job."""
        names = (
            "categories",
            "raw_records",
            "normalized_records",
            "products",
            "offers",
            "price_observations",
            "price_history",
            "source_runs",
            "ingest_jobs",
        )
        counts = {
            name: self.connection.scalar(sa.select(sa.func.count()).select_from(self.table(name)))
            for name in names
        }
        products = self.table("products")
        counts["products_with_category"] = self.connection.scalar(
            sa.select(sa.func.count())
            .select_from(products)
            .where(products.c.category_id.is_not(None))
        )
        return counts

    def duplicate_key_groups(self) -> dict[str, int]:
        """Считает SQL-группы технических дублей; семантическое равенство Product не угадывает."""
        keys = {
            "category_slugs": ("categories", ("slug",)),
            "category_source_ids": ("categories", ("source", "external_id")),
            "raw_records": ("raw_records", ("source_id", "source_key", "payload_sha256")),
            "normalized_records": ("normalized_records", ("raw_record_id", "normalizer_version")),
            "offers": ("offers", ("source_id", "source_offer_key")),
            "price_observations": ("price_observations", ("offer_id", "raw_record_id")),
        }
        result = {}
        for name, (table_name, fields) in keys.items():
            table = self.table(table_name)
            columns = [table.c[field] for field in fields]
            groups = (
                sa.select(*columns)
                .where(*(column.is_not(None) for column in columns))
                .group_by(*columns)
                .having(sa.func.count() > 1)
                .subquery()
            )
            result[name] = self.connection.scalar(sa.select(sa.func.count()).select_from(groups))
        return result

    def related(self, table_name: str, column: str, identifiers: list, *fields: str) -> list[dict]:
        """Читает trace пакетами, не превышая PostgreSQL parameter limit."""
        table = self.table(table_name)
        result = []
        for start in range(0, len(identifiers), 1000):
            query = sa.select(*(table.c[key] for key in fields) if fields else (table,)).where(
                table.c[column].in_(identifiers[start : start + 1000])
            )
            result.extend(dict(row) for row in self.connection.execute(query).mappings())
        return result

    def attach_offer(self, raw_id, product_id) -> None:
        """Привязывает Offer только если он ещё не имеет конфликтующего Product."""
        offers, observations = (self.table(n) for n in ("offers", "price_observations"))
        self.connection.execute(
            offers.update()
            .where(
                offers.c.product_id.is_(None),
                offers.c.status != "retired",
                offers.c.id.in_(
                    sa.select(observations.c.offer_id).where(observations.c.raw_record_id == raw_id)
                ),
            )
            .values(product_id=product_id, updated_at=datetime.now(UTC))
        )

    def directory_candidate(self, directory: str, name: str | None) -> UUID | None:
        """Переиспользует неподтверждённое имя без слияния юридических лиц или Product."""
        if not name:
            return None
        table = self.table(directory)
        key = self.connection.scalar(sa.select(sa.func.lower(name)))
        self.connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"catalog:{directory}:{key}"},
        )
        filters = [table.c.normalized_name == key, table.c.status == "candidate"]
        name_column = "canonical_name"
        if directory == "manufacturers":
            name_column = "legal_name"
            filters += [table.c.inn.is_(None), table.c.country_code.is_(None)]
        existing = self.connection.scalar(
            sa.select(table.c.id).where(*filters).order_by(table.c.created_at, table.c.id).limit(1)
        )
        if existing is not None:
            return existing
        return self.add(directory, normalized_name=key, **{name_column: name})["id"]

    def lock_record(self, record_id) -> None:
        """Сериализует ручные решения общего normalized record между разными jobs."""
        self.connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": str(record_id)},
        )

    def freeze_taxonomy(self) -> None:
        """Закрывает гонку SQL-проверки и назначения короткой SHARE-блокировкой дерева."""
        self.connection.execute(sa.text("LOCK TABLE categories IN SHARE MODE"))

    def assignable_path(self, category_id) -> dict | None:
        """Читает действительный путь назначения внутри транзакции решения."""
        table = self.table("categories")
        path, visited = [], set()
        while category_id:
            key = UUID(str(category_id))
            if key in visited:
                return None
            visited.add(key)
            row = (
                self.connection.execute(sa.select(table).where(table.c.id == key))
                .mappings()
                .first()
            )
            if not row or not row["is_active"] or row["review_status"] == "deprecated":
                return None
            path.append(dict(row))
            category_id = row["parent_id"]
        if not path or not path[0]["is_assignable"]:
            return None
        return {
            **path[0],
            "path_ids": [str(row["id"]) for row in reversed(path)],
            "path_names": [row["name"] for row in reversed(path)],
            "ancestor_exclusions": [
                row["semantic_exclusions"]
                for row in path[1:]
                if row["review_status"] == "approved" and row["semantic_exclusions"]
            ],
        }

    def has_manual_product_category(self, product: dict) -> bool:
        """Находит ручное назначение Product, в том числе из другой исходной строки."""
        decisions = self.table("classification_decisions")
        query = sa.select(
            sa.exists().where(
                decisions.c.product_id == product["id"],
                decisions.c.category_id == product["category_id"],
                decisions.c.status == "approved",
                decisions.c.method == "manual",
            )
        )
        return bool(self.connection.scalar(query))

    def assign_record_items(self, record_id, category_id, product_id) -> None:
        """Обновляет все представления одной записи и инвалидирует старые expected_version."""
        items, products = (self.table(n) for n in ("ingest_job_items", "products"))
        retired = sa.exists().where(
            products.c.id == items.c.product_id, products.c.status == "retired"
        )
        self.connection.execute(
            items.update()
            .where(
                items.c.normalized_record_id == record_id,
                ~items.c.issue_codes.contains(["archived_not_product"]),
                ~retired,
            )
            .values(
                category_id=category_id,
                product_id=product_id,
                status="assigned",
                processing_version=items.c.processing_version + 1,
                updated_at=datetime.now(UTC),
            )
        )

    def link_assigned_items(self, record_id, category_id, product_id, old_product_id=None) -> None:
        """Заполняет отсутствующую или отклонённую связь у строк той же категории."""
        items = self.table("ingest_job_items")
        self.connection.execute(
            items.update()
            .where(
                items.c.normalized_record_id == record_id,
                items.c.category_id == category_id,
                items.c.status == "assigned",
                items.c.product_id == old_product_id,
            )
            .values(product_id=product_id, processing_version=items.c.processing_version + 1)
        )

    def confirmed_identity_candidates(self, profile: dict) -> list[tuple]:
        """Возвращает ранее подтверждённые артикулы с точным исходным написанием."""
        from app.domain.catalog import normalized_key

        mpn = claim_value(profile, "mpn")
        role = "brand" if claim_value(profile, "brand") else "manufacturer"
        issuer = claim_value(profile, role)
        if not issuer or not mpn:
            return []
        identifiers, products = (self.table(n) for n in ("product_identifiers", "products"))
        query = (
            sa.select(products, identifiers.c.raw_value.label("identifier_value"))
            .join(identifiers, products.c.id == identifiers.c.product_id)
            .where(
                products.c.status == "identified",
                identifiers.c.identifier_type == "MPN",
                identifiers.c.normalized_value == normalized_key(mpn),
                identifiers.c.status == "confirmed",
                identifiers.c.issuer == normalized_key(issuer),
            )
            .order_by(identifiers.c.product_id)
            .limit(IDENTITY_LIMIT + 1)
        )
        result = []
        for row in self.connection.execute(query).mappings():
            product = dict(row)
            if text_key(product.pop("identifier_value")) != text_key(mpn):
                continue
            anchor = identity_profile({"name": product["canonical_name"], role: issuer, "mpn": mpn})
            known = self.product_profile(product)
            if not identity_conflicts(known, anchor):
                result.append((product, accumulate_profile(known, anchor)))
        return result

    def product_profile(self, product: dict) -> dict:
        """Вычисляет профиль старой карточки без повышения доверия к гипотезам."""
        attributes = product["attributes"]
        stored = attributes.get("identity_profile")
        if stored and stored.get("version") == 2:
            return stored
        from_answer = identity_profile(
            {"name": product["canonical_name"]}, attributes.get("understanding")
        )
        return accumulate_profile(stored, from_answer) if stored else from_answer

    def approved_brand_aliases(self, terms=None) -> dict[str, str]:
        """Читает только алиасы, утверждённые для активной записи справочника."""
        aliases, brands = (self.table(n) for n in ("brand_aliases", "brands"))
        query = (
            sa.select(aliases.c.alias, brands.c.canonical_name)
            .join(brands, brands.c.id == aliases.c.brand_id)
            .where(aliases.c.status == "approved", brands.c.status == "approved")
        )
        if terms is not None:
            terms = sorted({text_key(term) for term in terms if term})
            if not terms:
                return {}
            query = query.where(aliases.c.normalized_alias.in_(terms))
        return {text_key(row.alias): row.canonical_name for row in self.connection.execute(query)}

    def reusable_product_classification(self, product_id) -> dict | None:
        """Читает стабильное решение без категории при неизменном дереве."""
        decisions = self.table("classification_decisions")
        query = (
            sa.select(decisions)
            .where(
                decisions.c.product_id == product_id,
                decisions.c.method == "retrieval_vlm",
                decisions.c.taxonomy_revision == self.taxonomy_revision(),
            )
            .order_by(decisions.c.created_at.desc(), decisions.c.id)
            .limit(1)
        )
        row = self.connection.execute(query).mappings().first()
        if (
            row
            and row["status"] in {"needs_review", "no_suitable_category"}
            and row["evidence"].get("identity_reusable")
        ):
            return dict(row)
        return None

    def confirmed_product(self, fields: dict) -> dict | None:
        """Поддерживает строгий поиск legacy-реквизитов через общую проверку профиля."""
        row, _ = self.find_identity_product(identity_profile(fields), structured_only=True)
        known = row["attributes"].get("identity_fields", {}) if row else {}
        if known.get("quantity") and fields.get("unit") and fields["unit"] != known.get("unit"):
            return None
        return row

    def find_identity_product(
        self, profile: dict, excluded=(), *, structured_only: bool = False
    ) -> tuple[dict | None, dict]:
        """Захватывает ограниченные поисковые ключи до чтения и записи карточки."""
        lookup = identity_retrieval_keys(profile)
        if lookup:
            self.connection.execute(
                sa.text("""SELECT pg_advisory_xact_lock(lock_id) FROM (
                  SELECT DISTINCT hashtextextended('product-identity:' || key, 0) AS lock_id
                  FROM unnest(CAST(:keys AS text[])) AS items(key) ORDER BY lock_id
                ) AS ordered"""),
                {"keys": lookup},
            )
        products = self.table("products")
        candidates = {
            p["id"]: (p, facts) for p, facts in self.confirmed_identity_candidates(profile)
        }
        if lookup and not structured_only:
            ids = (
                self.connection.execute(
                    sa.text("""SELECT DISTINCT found.product_id
                  FROM unnest(CAST(:keys AS text[])) terms(key)
                  CROSS JOIN LATERAL (
                    SELECT k.product_id FROM product_identity_keys k
                    JOIN products p ON p.id=k.product_id AND p.status <> 'retired'
                    WHERE k.identity_key=terms.key ORDER BY k.product_id LIMIT :limit
                  ) found ORDER BY found.product_id LIMIT :limit"""),
                    {"keys": lookup, "limit": IDENTITY_LIMIT + 1},
                )
                .scalars()
                .all()
            )
            query = sa.select(products).where(products.c.id.in_(ids)).order_by(products.c.id)
            for row in self.connection.execute(query).mappings():
                product = dict(row)
                candidates.setdefault(product["id"], (product, self.product_profile(product)))
        for pid in excluded:
            candidates.pop(pid, None)
        aliases = (
            self.approved_brand_aliases(
                [claim_value(facts, "brand") for _, facts in candidates.values()]
                + [claim_value(profile, "brand")]
            )
            if candidates
            else {}
        )
        decision = decide_identity(
            canonical_brand(profile, aliases),
            [
                (pid, canonical_brand(facts, aliases))
                for pid, (_, facts) in list(candidates.items())[:IDENTITY_LIMIT]
            ],
            overflow=len(candidates) > IDENTITY_LIMIT,
        )
        selected = decision.pop("product_id")
        return candidates[selected][0] if selected else None, decision

    def remember_identity(self, product_id, profile: dict) -> None:
        """Пишет исходные факты в пустые поля и актуализирует индекс Product."""
        product = self.get("products", product_id, lock=True)
        attributes = product["attributes"]
        previous = attributes.get("identity_profile")
        corrected = set()
        if product["status"] != "identified" and any(
            claim.get("evidence") == "verified_document" for claim in profile["claims"].values()
        ):
            previous = previous or self.product_profile(product)
            refined = refine_legacy_roles(previous, profile)
            corrected = previous["claims"].keys() - refined["claims"].keys()
            previous = refined
        if previous and identity_conflicts(previous, profile):
            return
        if previous:
            profile = accumulate_profile(previous, profile)
        updates = {}
        if attributes.get("identity_profile") != profile:
            updates["attributes"] = {**attributes, "identity_profile": profile}
        for field, directory in (("brand", "brands"), ("manufacturer", "manufacturers")):
            if (not product[f"{field}_id"] or field in corrected) and (
                value := claim_value(profile, field)
            ):
                updates[f"{field}_id"] = self.directory_candidate(directory, value)
        if (not product["mpn"] or "mpn" in corrected) and (mpn := claim_value(profile, "mpn")):
            updates.update(mpn=mpn, normalized_mpn=searchable_mpn(profile))
        if not product["model"] and (model := claim_value(profile, "model")):
            updates["model"] = model
        if updates:
            self.update("products", product_id, **updates)
        keys = self.table("product_identity_keys")
        desired = identity_retrieval_keys(profile)
        self.connection.execute(
            keys.delete().where(
                keys.c.product_id == product["id"], keys.c.identity_key.not_in(desired)
            )
        )
        if desired:
            self.connection.execute(
                insert(keys)
                .values([{"identity_key": key, "product_id": product["id"]} for key in desired])
                .on_conflict_do_nothing()
            )

    def product_identity_rows(self) -> list[dict]:
        """Читает сохранённые Product, исходные поля и последнее валидное answer_1."""
        p, n, items, answers, brands, makers = (
            self.table(table)
            for table in (
                "products",
                "normalized_records",
                "ingest_job_items",
                "product_understandings",
                "brands",
                "manufacturers",
            )
        )
        links = (
            sa.select(
                items.c.product_id,
                items.c.normalized_record_id,
                items.c.raw_record_id,
                items.c.status,
            )
            .where(items.c.product_id.is_not(None), items.c.normalized_record_id.is_not(None))
            .distinct()
            .subquery()
        )
        raw, obs, offers, sellers, categories = (
            self.table(name)
            for name in ("raw_records", "price_observations", "offers", "sellers", "categories")
        )
        decisions = self.table("classification_decisions")
        category_weight = (
            sa.select(
                sa.func.max(
                    sa.case(
                        (decisions.c.method.in_(["approved_rule", "approved_mapping"]), 2),
                        (decisions.c.method == "retrieval_vlm", 1),
                        else_=0,
                    )
                )
            )
            .where(
                decisions.c.product_id == p.c.id,
                decisions.c.category_id == p.c.category_id,
                decisions.c.status == "approved",
            )
            .correlate(p)
            .scalar_subquery()
        )
        understanding = (
            sa.select(answers.c.answer_json)
            .where(
                answers.c.normalized_record_id == n.c.id,
                answers.c.status == "recognized",
                answers.c.answer_json.is_not(None),
                ~answers.c.model_revision.startswith("mock"),
            )
            .order_by(answers.c.created_at.desc(), answers.c.id)
            .limit(1)
            .lateral()
        )
        query = (
            sa.select(
                p.c.id.label("product_id"),
                p.c.canonical_name,
                p.c.status.label("product_status"),
                p.c.created_at,
                p.c.updated_at,
                categories.c.id.label("category_id"),
                sa.func.coalesce(category_weight, 0).label("category_weight"),
                p.c.mpn,
                p.c.model,
                p.c.unit_code,
                p.c.attributes["understanding"].label("stored_answer"),
                p.c.attributes["identity_profile"].label("stored_profile"),
                n.c.attributes["fields"].label("fields"),
                understanding.c.answer_json.label("answer"),
                brands.c.canonical_name.label("brand_name"),
                makers.c.legal_name.label("manufacturer_name"),
                raw.c.id.label("raw_id"),
                raw.c.origin_url,
                links.c.status.label("item_status"),
                offers.c.id.label("offer_id"),
                sellers.c.id.label("seller_id"),
                sellers.c.legal_name.label("seller_name"),
            )
            .outerjoin(links, links.c.product_id == p.c.id)
            .outerjoin(n, n.c.id == links.c.normalized_record_id)
            .outerjoin(raw, raw.c.id == links.c.raw_record_id)
            .outerjoin(obs, obs.c.raw_record_id == raw.c.id)
            .outerjoin(offers, offers.c.id == obs.c.offer_id)
            .outerjoin(sellers, sellers.c.id == offers.c.seller_id)
            .outerjoin(
                categories,
                sa.and_(
                    categories.c.id == p.c.category_id,
                    categories.c.is_active,
                    categories.c.is_assignable,
                ),
            )
            .outerjoin(understanding, sa.true())
            .outerjoin(
                brands,
                sa.and_(
                    brands.c.id == p.c.brand_id, brands.c.status.in_(["approved", "candidate"])
                ),
            )
            .outerjoin(
                makers,
                sa.and_(
                    makers.c.id == p.c.manufacturer_id,
                    makers.c.status.in_(["approved", "candidate"]),
                ),
            )
            .where(p.c.status != "retired")
            .order_by(p.c.created_at, p.c.id, n.c.id, raw.c.id, offers.c.id)
        )
        return [dict(r) for r in self.connection.execute(query).mappings()]

    def product_identity_manual(self) -> list[dict]:
        """Читает действующие ручные категории и запреты на объединение."""
        c, m, items = (
            self.table(table)
            for table in ("classification_decisions", "product_match_decisions", "ingest_job_items")
        )
        categories = (
            sa.select(
                sa.func.coalesce(c.c.product_id, items.c.product_id).label("product_id"),
                c.c.category_id,
                c.c.category_id.is_(None).label("blocked"),
            )
            .outerjoin(items, c.c.normalized_record_id == items.c.normalized_record_id)
            .where(c.c.method == "manual", c.c.status == "approved")
        )
        latest_match = (
            sa.select(m.c.product_id, m.c.status, m.c.normalized_record_id)
            .where(m.c.stage == "manual")
            .ext(distinct_on(m.c.normalized_record_id, m.c.product_id))
            .order_by(m.c.normalized_record_id, m.c.product_id, m.c.created_at.desc(), m.c.id)
            .subquery()
        )
        denied_target = sa.select(
            latest_match.c.product_id,
            sa.cast(sa.null(), sa.Uuid).label("category_id"),
            sa.true().label("blocked"),
        ).where(latest_match.c.status == "rejected")
        denied_source = (
            sa.select(
                items.c.product_id,
                sa.cast(sa.null(), sa.Uuid).label("category_id"),
                sa.true().label("blocked"),
            )
            .join(latest_match, latest_match.c.normalized_record_id == items.c.normalized_record_id)
            .where(latest_match.c.status == "rejected", items.c.product_id.is_not(None))
        )
        query = categories.union(denied_target, denied_source)
        return [dict(r) for r in self.connection.execute(query).mappings()]

    def unlinked_identity_rows(self) -> list[dict]:
        """Читает только проверенные оператором или завершённые позиции без Product."""
        items, raw, normalized, answers, matches, decisions, obs, offers = (
            self.table(name)
            for name in (
                "ingest_job_items",
                "raw_records",
                "normalized_records",
                "product_understandings",
                "product_match_decisions",
                "classification_decisions",
                "price_observations",
                "offers",
            )
        )
        answer = (
            sa.select(answers.c.answer_json)
            .where(
                answers.c.normalized_record_id == normalized.c.id,
                answers.c.status.in_(["recognized", "insufficient_evidence"]),
                answers.c.answer_json.is_not(None),
                ~answers.c.model_revision.startswith("mock"),
            )
            .order_by(answers.c.created_at.desc(), answers.c.id)
            .limit(1)
            .lateral()
        )
        query = (
            sa.select(
                items.c.id,
                items.c.raw_record_id,
                items.c.normalized_record_id,
                items.c.status,
                items.c.category_id,
                items.c.processing_version,
                raw.c.origin_url,
                normalized.c.attributes["fields"].label("fields"),
                answer.c.answer_json.label("answer"),
                offers.c.id.label("offer_id"),
            )
            .join(raw, items.c.raw_record_id == raw.c.id)
            .join(normalized, normalized.c.id == items.c.normalized_record_id)
            .join(obs, obs.c.raw_record_id == raw.c.id)
            .join(offers, obs.c.offer_id == offers.c.id)
            .join(answer, sa.true())
            .where(
                items.c.product_id.is_(None),
                offers.c.product_id.is_(None),
                items.c.status.in_(["assigned", "needs_review"]),
                ~sa.exists().where(
                    matches.c.normalized_record_id == normalized.c.id, matches.c.stage == "manual"
                ),
                ~sa.exists().where(
                    decisions.c.normalized_record_id == normalized.c.id,
                    decisions.c.method == "manual",
                    decisions.c.status == "approved",
                ),
            )
            .order_by(items.c.id, offers.c.id)
        )
        return [dict(row) for row in self.connection.execute(query).mappings()]

    def attach_merged_source(self, match: dict) -> None:
        """Восстанавливает связь исходника, сохраняя рабочий статус и решения о категории."""
        target = UUID(match["product_id"])
        self.update(
            "ingest_job_items",
            match["id"],
            product_id=target,
            processing_version=match["processing_version"] + 1,
        )
        offers = self.table("offers")
        updated = self.connection.execute(
            offers.update()
            .where(
                offers.c.id == UUID(match["offer_id"]),
                offers.c.product_id.is_(None),
                offers.c.status != "retired",
            )
            .values(product_id=target)
        ).rowcount
        if updated != 1:
            raise ConflictError("Предложение исходной позиции изменилось после preview")
        self.add(
            "product_match_decisions",
            normalized_record_id=UUID(match["normalized_record_id"]),
            product_id=target,
            stage="after_understanding",
            method="verified_saved_source",
            status="confirmed",
            evidence={"proof": match["proof"]},
        )

    def product_merge_aliases(self) -> list[dict]:
        """Предлагает очистить только retired с действующим ранее записанным редиректом."""
        aliases, products = (self.table(n) for n in ("product_merge_redirects", "products"))
        target = products.alias("live")
        query = (
            sa.select(
                aliases.c.old_product_id,
                aliases.c.product_id,
                aliases.c.evidence,
                products.c.updated_at,
                target.c.updated_at.label("target_updated_at"),
            )
            .join(products, aliases.c.old_product_id == products.c.id)
            .join(target, aliases.c.product_id == target.c.id)
            .where(products.c.status == "retired", target.c.status != "retired")
            .order_by(aliases.c.old_product_id)
        )
        return json_value([dict(row) for row in self.connection.execute(query).mappings()])

    def lock_product_merge(self) -> None:
        """Блокирует запуск новых заданий и изменения Product на время короткой операции."""
        self.freeze_taxonomy()
        self.connection.execute(sa.text("LOCK TABLE ingest_jobs IN SHARE ROW EXCLUSIVE MODE"))
        jobs = self.table("ingest_jobs")
        if self.connection.scalar(
            sa.select(
                sa.exists().where(
                    jobs.c.status.in_(["running", "queued", "cancel_requested", "validating"])
                )
            )
        ):
            raise ConflictError("Дождитесь остановки или завершения всех активных заданий")
        self.connection.execute(
            sa.text("""LOCK TABLE products, ingest_job_items,
            offers, product_identifiers, product_identity_keys, product_merge_redirects,
            product_match_decisions, classification_decisions, review_tasks, taxonomy_gaps
            IN SHARE ROW EXCLUSIVE MODE NOWAIT""")
        )

    def merge_integrity(self) -> dict:
        """Фиксирует число исходников, предложений, цен и неизменяемый прогресс заданий."""
        counts = self.catalog_counts()
        counts.pop("products")
        counts.pop("products_with_category")
        jobs = self.table("ingest_jobs")
        return {
            **counts,
            "sellers": self.connection.scalar(
                sa.select(sa.func.count()).select_from(self.table("sellers"))
            ),
            "sha256": {
                name: self.merge_digest(name, ("product_id",) if name == "offers" else ())
                for name in (
                    "raw_records",
                    "normalized_records",
                    "product_understandings",
                    "offers",
                    "price_observations",
                    "price_history",
                )
            },
            "jobs": json_value(
                [
                    dict(row)
                    for row in self.connection.execute(
                        sa.select(
                            jobs.c.id,
                            jobs.c.status,
                            jobs.c.total_count,
                            jobs.c.processed_count,
                            jobs.c.assigned_count,
                            jobs.c.cancel_requested,
                        ).order_by(jobs.c.id)
                    ).mappings()
                ]
            ),
        }

    def merge_digest(self, table: str, excluded: tuple = ()) -> str:
        """Сверяет множества UUID и все сохраняемые значения без выгрузки строк."""
        rows = self.table(table)
        body = sa.func.to_jsonb(rows.table_valued())
        if excluded:
            body = body.op("-")(sa.cast(list(excluded), sa.ARRAY(sa.Text)))
        row_hash = sa.func.sha256(sa.func.convert_to(sa.cast(body, sa.Text), "UTF8"))
        ordered = sa.func.aggregate_strings(sa.func.encode(row_hash, "hex"), "").aggregate_order_by(
            *rows.primary_key.columns
        )
        hashed = sa.func.sha256(sa.func.convert_to(sa.func.coalesce(ordered, ""), "UTF8"))
        return self.connection.scalar(sa.select(sa.func.encode(hashed, "hex")).select_from(rows))

    def merge_offer_routes(self, mapping: dict, attachments: dict | None = None) -> str:
        """Хеширует будущие либо текущие связи предложений с Product по UUID."""
        query = sa.text("""SELECT encode(sha256(convert_to(coalesce(string_agg(
            id::text || ':' || coalesce(CAST(:attachments AS jsonb)->>id::text,
                                        CAST(:mapping AS jsonb)->>product_id::text,
                                        product_id::text, '-'),
            ',' ORDER BY id), ''), 'UTF8')), 'hex') FROM offers""")
        import json

        return self.connection.scalar(
            query, {"mapping": json.dumps(mapping), "attachments": json.dumps(attachments or {})}
        )

    def merge_candidate_routes(self, mapping: dict) -> None:
        """Переносит оперативные ссылки кандидатов и оставляет прежние UUID в аудите."""
        if not mapping:
            return
        decisions = self.table("product_match_decisions")
        query = sa.select(
            decisions.c.id, decisions.c.candidate_product_ids, decisions.c.evidence
        ).where(
            decisions.c.candidate_product_ids.op("?|")(sa.cast(list(mapping), sa.ARRAY(sa.Text)))
        )
        for row in self.connection.execute(query).mappings():
            candidates = row["candidate_product_ids"]
            evidence = row["evidence"]
            history = evidence.get("merge_candidate_ids_before", [])
            self.update(
                "product_match_decisions",
                row["id"],
                candidate_product_ids=list(
                    dict.fromkeys(mapping.get(pid, pid) for pid in candidates)
                ),
                evidence={**evidence, "merge_candidate_ids_before": [*history, candidates]},
            )

    def merge_products(self, group: dict) -> dict[str, int]:
        """Переносит связанные записи, сохраняет старые UUID как aliases и аудирует перенос."""
        target = UUID(group["survivor"])
        absorbed = [UUID(key) for key in group["duplicates"]]
        category = group.get("category_id")
        if category:
            self.update("products", target, category_id=UUID(category))
        if group.get("profile"):
            self.remember_identity(target, group["profile"])
        moved = {}
        for table in (
            "offers",
            "ingest_job_items",
            "product_match_decisions",
            "classification_decisions",
            "review_tasks",
            "taxonomy_gaps",
        ):
            ref = self.table(table)
            values = {"product_id": target}
            if table == "ingest_job_items":
                values["processing_version"] = ref.c.processing_version + 1
            moved[table] = self.connection.execute(
                ref.update().where(ref.c.product_id.in_(absorbed)).values(**values)
            ).rowcount
        if category:
            items = self.table("ingest_job_items")
            self.connection.execute(
                items.update()
                .where(
                    items.c.product_id == target,
                    items.c.status == "assigned",
                    items.c.category_id.is_distinct_from(UUID(category)),
                )
                .values(
                    category_id=UUID(category), processing_version=items.c.processing_version + 1
                )
            )
        identifiers = self.table("product_identifiers")
        identifier_evidence = {}
        for item in self.connection.execute(
            sa.select(identifiers)
            .where(identifiers.c.product_id.in_(absorbed))
            .order_by(identifiers.c.created_at, identifiers.c.id)
        ).mappings():
            duplicate = (
                self.connection.execute(
                    sa.select(identifiers).where(
                        identifiers.c.product_id == target,
                        identifiers.c.identifier_type == item["identifier_type"],
                        identifiers.c.issuer.is_not_distinct_from(item["issuer"]),
                        identifiers.c.normalized_value == item["normalized_value"],
                    )
                )
                .mappings()
                .first()
            )
            if duplicate is None:
                self.update("product_identifiers", item["id"], product_id=target)
            else:
                statuses = {item["status"], duplicate["status"]}
                if statuses == {"rejected", "confirmed"}:
                    raise ConflictError("Противоречащие ручные решения по идентификатору")
                identifier_evidence.setdefault(str(item["product_id"]), []).append(
                    json_value(dict(item))
                )
                if duplicate["status"] == "candidate" and item["status"] != "candidate":
                    self.update("product_identifiers", duplicate["id"], status=item["status"])
                self.connection.execute(identifiers.delete().where(identifiers.c.id == item["id"]))
        redirects = self.table("product_merge_redirects")
        self.connection.execute(
            redirects.update().where(redirects.c.product_id.in_(absorbed)).values(product_id=target)
        )
        if not group.get("legacy"):
            self.connection.execute(
                redirects.insert().values(
                    [
                        {
                            "old_product_id": key,
                            "product_id": target,
                            "evidence": {
                                "identity_key": group["key"],
                                "proofs": group["proofs"],
                                "collapsed_identifiers": identifier_evidence.get(str(key), []),
                            },
                        }
                        for key in absorbed
                    ]
                )
            )
        elif identifier_evidence:
            self.connection.execute(
                redirects.update()
                .where(redirects.c.old_product_id == absorbed[0])
                .values(
                    evidence=redirects.c.evidence.op("||")(
                        {"collapsed_identifiers": identifier_evidence[str(absorbed[0])]}
                    ),
                )
            )
        keys = self.table("product_identity_keys")
        self.connection.execute(keys.delete().where(keys.c.product_id.in_(absorbed)))
        products = self.table("products")
        deleted = self.connection.execute(
            products.delete().where(products.c.id.in_(absorbed))
        ).rowcount
        if deleted != len(absorbed):
            raise ConflictError("Число удаляемых карточек изменилось после preview")
        return moved

    def category_references(self, category_id) -> dict:
        """Считает текущие ссылки для проверяемого preview изменений дерева."""
        references = {}
        for name, column in (
            ("products", "category_id"),
            ("category_source_mappings", "category_id"),
            ("category_aliases", "category_id"),
            ("ingest_job_items", "category_id"),
            ("classification_decisions", "category_id"),
        ):
            table = self.table(name)
            references[name] = self.connection.scalar(
                sa.select(sa.func.count()).select_from(table).where(table.c[column] == category_id)
            )
        return references

    def active_category_products(self, category_id) -> int:
        """Считает только доступные товары, которые потребуют переназначения."""
        products = self.table("products")
        return self.connection.scalar(
            sa.select(sa.func.count())
            .select_from(products)
            .where(products.c.category_id == category_id, products.c.status != "retired")
        )

    def retire_product(self, product_id) -> dict:
        """Скрывает товар и его предложения, сохраняя UUID и историю загрузок."""
        offers, items = (self.table(name) for name in ("offers", "ingest_job_items"))
        self.update("products", product_id, status="retired")
        retired_offers = self.connection.execute(
            offers.update()
            .where(offers.c.product_id == product_id, offers.c.status != "retired")
            .values(status="retired", updated_at=datetime.now(UTC))
        ).rowcount
        linked_items = self.connection.scalar(
            sa.select(sa.func.count()).select_from(items).where(items.c.product_id == product_id)
        )
        return {"offers_archived": retired_offers, "linked_items": linked_items}

    def unarchived_not_products(self, job_id, lock: bool = False) -> list[dict]:
        """Выбирает позиции «Не товар» текущего задания, не затрагивая его историю."""
        items = self.table("ingest_job_items")
        query = (
            sa.select(items)
            .where(
                items.c.job_id == UUID(str(job_id)),
                items.c.status == "not_product",
                ~items.c.issue_codes.contains(["archived_not_product"]),
            )
            .order_by(items.c.id)
        )
        if lock:
            query = query.with_for_update()
        return [dict(row) for row in self.connection.execute(query).mappings()]

    def lock_taxonomy_change(self) -> None:
        """Сериализует утверждённый changeset с произвольным SQL изменением дерева."""
        self.connection.execute(sa.text("LOCK TABLE categories IN SHARE ROW EXCLUSIVE MODE"))

    def apply_taxonomy_change(self, change: dict, actor: str) -> None:
        """Сохраняет старый UUID неактивным, переносит текущие ссылки и пишет redirect."""
        old_id, target_id = change["category_id"], change["target_category_id"]
        action = change["action"]
        if action == "keep":
            return
        old = self.get("categories", old_id)
        if target_id:
            if action == "remove_parent_reparent":
                self.update("categories", target_id, parent_id=old["parent_id"])
            for child in self.all("categories", parent_id=old_id):
                if child["id"] != target_id:
                    self.update("categories", child["id"], parent_id=target_id)
            for name in ("products", "category_source_mappings"):
                for row in self.all(name, category_id=old_id):
                    self.update(name, row["id"], category_id=target_id)
            for alias in self.all("category_aliases", category_id=old_id):
                if not self.all(
                    "category_aliases",
                    category_id=target_id,
                    normalized_alias=alias["normalized_alias"],
                ):
                    self.update("category_aliases", alias["id"], category_id=target_id)
            for decision in self.all("classification_decisions", category_id=old_id):
                if decision["status"] in {"approved", "proposed"}:
                    self.update("classification_decisions", decision["id"], status="superseded")
                if decision["status"] == "approved":
                    self.add(
                        "classification_decisions",
                        normalized_record_id=decision["normalized_record_id"],
                        product_id=decision["product_id"],
                        category_id=target_id,
                        method="manual",
                        selection_type="broader",
                        status="approved",
                        evidence={
                            "change_set_id": str(change["id"]),
                            "previous_decision_id": str(decision["id"]),
                            "reason": change["reason"],
                        },
                        decided_by=actor,
                    )
            for item in self.all("ingest_job_items", category_id=old_id):
                self.update(
                    "ingest_job_items",
                    item["id"],
                    category_id=target_id,
                    processing_version=item["processing_version"] + 1,
                )
                self.event(
                    item["job_id"],
                    "taxonomy.redirected",
                    "Утверждён changeset категории",
                    item_id=item["id"],
                    data={"change_set_id": str(change["id"]), "category_id": str(target_id)},
                )
            table = self.table("category_redirects")
            self.connection.execute(
                insert(table)
                .values(
                    old_category_id=old_id, new_category_id=target_id, change_set_id=change["id"]
                )
                .on_conflict_do_update(
                    index_elements=[table.c.old_category_id],
                    set_={"new_category_id": target_id, "change_set_id": change["id"]},
                )
            )
        self.update("categories", old_id, is_active=False, review_status="deprecated")

    def approved_category_hint(
        self, raw_id, fields: dict, understanding: dict
    ) -> tuple[object, str] | None:
        """Читает только approved source mappings/aliases; неоднозначность не разрешает догадкой."""
        from app.domain.catalog import normalized_key

        raw = self.get("raw_records", raw_id)
        path = fields.get("category_path")
        mappings = (
            self.all(
                "category_source_mappings",
                source_id=raw["source_id"],
                external_category_key=" > ".join(path),
                status="approved",
            )
            if path
            else []
        )
        ids = {row["category_id"] for row in mappings if row["category_id"]}
        if len(ids) == 1:
            return ids.pop(), "approved_mapping"
        aliases = self.all(
            "category_aliases",
            normalized_alias=normalized_key(understanding["product_type"]),
            status="approved",
        )
        ids = {row["category_id"] for row in aliases}
        return (ids.pop(), "approved_rule") if len(ids) == 1 else None

    def product_candidates(self, fields: dict) -> list[dict]:
        """Возвращает кандидатов по MPN, не объявляя совпадение идентичностью."""
        from app.domain.catalog import normalized_key

        if not fields.get("mpn"):
            return []
        table = self.table("products")
        statement = (
            sa.select(table)
            .where(
                table.c.normalized_mpn == normalized_key(fields["mpn"]), table.c.status != "retired"
            )
            .limit(20)
        )
        return [dict(row) for row in self.connection.execute(statement).mappings()]
