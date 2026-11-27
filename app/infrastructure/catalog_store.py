# app/infrastructure/catalog_store.py
"""Транзакционный SQL-адаптер импорта; таблицы отражаются из применённых миграций."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from app.domain.catalog import CatalogError, ConflictError, fingerprint, json_value


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
        rows = self.connection.execute(
            self.products_query()
            .where(*conditions)
            .order_by(order, table.c.id)
            .offset(offset)
            .limit(limit)
        ).mappings()
        return {
            "items": [dict(row) for row in rows],
            "total": count,
            "offset": offset,
            "limit": limit,
        }

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
        self.populate_price_history(product_id, rows, history_offset, history_limit)
        return {"items": rows, "total": total, "offset": offset, "limit": limit}

    def populate_price_history(
        self, product_id: UUID, offers: list[dict], offset: int, limit: int
    ) -> None:
        """Возвращает исходные наблюдения и сведения о верификации с сохранением дат."""
        if not offers:
            return
        obs, history, items = (
            self.table(n) for n in ("price_observations", "price_history", "ingest_job_items")
        )
        by_offer = {row["id"]: row for row in offers}
        links = sa.exists().where(items.c.raw_record_id == obs.c.raw_record_id)
        matching = links.where(items.c.product_id == product_id)
        linked = links.where(items.c.product_id.is_not(None))
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
                sa.or_(~linked, matching),
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

    def confirmed_product(self, fields: dict) -> dict | None:
        """Совпадение MPN допустимо только с подтверждённым issuer, без конфликтов вариантов."""
        from app.domain.catalog import identity_features, normalized_key

        issuer = fields.get("manufacturer") or fields.get("brand")
        if not issuer or not fields.get("mpn"):
            return None
        ids = self.all(
            "product_identifiers",
            identifier_type="MPN",
            normalized_value=normalized_key(fields["mpn"]),
            status="confirmed",
            issuer=normalized_key(issuer),
        )
        candidates = {row["product_id"] for row in ids}
        if len(candidates) != 1:
            return None
        product = self.get("products", candidates.pop())
        if product["status"] != "identified":
            return None
        known = product["attributes"].get("identity_fields", {})
        if any(fields.get(k) and known.get(k) != fields[k] for k in ("unit", "quantity")):
            return None
        given_features = identity_features(fields.get("name"))
        known_features = identity_features(product["canonical_name"])
        if any(known_features.get(key) != value for key, value in given_features.items()):
            return None
        return product

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
