# app/infrastructure/database.py
"""PostgreSQL data access: ограничения, атомарная запись и неизменяемый аудит снимков."""

from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import URL, Engine

from app.domain.planning import ImportPlan
from app.domain.taxonomy import Permission, Review, Snapshot, TaxonomyError, snapshot_hash
from app.settings import Settings

metadata = sa.MetaData()
snapshots = sa.Table(
    "taxonomy_snapshots",
    metadata,
    sa.Column("sha256", sa.String(64), primary_key=True),
    sa.Column("source", sa.String(64), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
)
categories = sa.Table(
    "categories",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("parent_id", sa.Uuid, sa.ForeignKey("categories.id", ondelete="RESTRICT")),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("slug", sa.String(160), nullable=False, unique=True),
    sa.Column("sort_order", sa.Integer, nullable=False),
    sa.Column("source", sa.String(64), nullable=False),
    sa.Column("external_id", sa.Text),
    sa.Column("source_url", sa.Text),
    sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("quality_flags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("is_assignable", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("review_status", sa.Text, nullable=False, server_default="provisional"),
    sa.Column("semantic_description", sa.Text),
    sa.Column("semantic_purpose", sa.Text),
    sa.Column("semantic_exclusions", sa.Text),
    sa.CheckConstraint(
        "review_status IN ('provisional','approved','deprecated')",
        name="categories_review_status_check",
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column("imported_values", JSONB),
    sa.Column("snapshot_sha256", sa.String(64), sa.ForeignKey("taxonomy_snapshots.sha256")),
    sa.UniqueConstraint("source", "external_id", name="uq_categories_source_external"),
    sa.CheckConstraint("length(trim(name)) > 0", name="ck_categories_name"),
    sa.CheckConstraint("sort_order >= 0", name="ck_categories_sort_order"),
    sa.CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_categories_self_parent"),
    sa.CheckConstraint("source = 'custom' OR external_id IS NOT NULL", name="ck_categories_key"),
    sa.CheckConstraint("jsonb_typeof(quality_flags) = 'array'", name="ck_categories_quality_flags"),
    sa.Index("ix_categories_parent_id", "parent_id"),
)
import_runs = sa.Table(
    "taxonomy_import_runs",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "snapshot_sha256", sa.String(64), sa.ForeignKey("taxonomy_snapshots.sha256"), nullable=False
    ),
    sa.Column("source", sa.String(64), nullable=False),
    sa.Column("commit_sha", sa.String(64), nullable=False),
    sa.Column("result", JSONB, nullable=False),
    sa.Column("review", JSONB, nullable=True),
    sa.Column("permission", JSONB),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
)


def create_db_engine(settings: Settings) -> Engine:
    """Собирает безопасный URL драйвера и ограничивает connect/statement/lock timeouts."""
    password = settings.postgres_password.get_secret_value()
    if not password or password in {"CHANGE_ME", "password"}:
        raise TaxonomyError("POSTGRES_PASSWORD обязателен; храните реальное значение только в .env")
    return sa.create_engine(
        URL.create(
            "postgresql+psycopg",
            username=settings.postgres_user,
            password=password,
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=settings.postgres_db,
        ),
        connect_args={
            "connect_timeout": settings.db_connect_timeout,
            "options": (
                f"-c statement_timeout={settings.db_statement_timeout_ms} -c lock_timeout=5000"
            ),
        },
        pool_pre_ping=True,
        hide_parameters=True,
    )


def serializable_row(row: dict) -> dict:
    """Приводит UUID к JSON-представлению для чистого сравнения imported baseline."""
    return {key: str(value) if isinstance(value, UUID) else value for key, value in row.items()}


class SqlImportUnitOfWork:
    """Одна транзакция импорта с PostgreSQL advisory lock и rollback по умолчанию."""

    def __init__(self, engine: Engine, *, readonly: bool = False) -> None:
        """Сохраняет engine и режим; соединение создаётся только при входе в операцию."""
        self.engine = engine
        self.readonly = readonly

    def __enter__(self) -> "SqlImportUnitOfWork":
        """Сериализует импорты; dry-run использует read-only repeatable-read транзакцию."""
        self.connection = self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ" if self.readonly else "READ COMMITTED"
        )
        self.transaction = self.connection.begin()
        try:
            if self.readonly:
                self.connection.execute(sa.text("SET TRANSACTION READ ONLY"))
            else:
                self.connection.execute(sa.text("SELECT pg_advisory_xact_lock(16842391)"))
        except BaseException:
            self.connection.close()
            raise
        return self

    def __exit__(self, *args: object) -> None:
        """Откатывает любые незавершённые изменения и гарантированно закрывает connection."""
        try:
            if self.transaction.is_active:
                self.transaction.rollback()
        finally:
            self.connection.close()

    def list_categories(self) -> list[dict]:
        """Блокирует существующие строки apply, защищая baseline от параллельных правок."""
        query = sa.select(categories)
        if not self.readonly:
            query = query.with_for_update()
        return [serializable_row(dict(row)) for row in self.connection.execute(query).mappings()]

    def persist(
        self,
        snapshot: Snapshot,
        review: Review | None,
        permission: Permission | None,
        plan: ImportPlan,
        commit_sha: str,
    ) -> str:
        """Сохраняет снимок, изменения категорий и счётчики в одной transaction boundary."""
        sha = snapshot_hash(snapshot)
        self.connection.execute(
            insert(snapshots)
            .values(
                sha256=sha,
                source=snapshot.source.key,
                payload=snapshot.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=[snapshots.c.sha256])
        )
        operations = [(True, v) for v in plan.added] + [(False, v) for v in plan.updated]
        levels = {str(n.id): n.level for n in snapshot.nodes}
        for is_new, values in sorted(operations, key=lambda pair: levels[pair[1]["id"]]):
            persisted = {**values, "id": UUID(values["id"])}
            persisted["parent_id"] = UUID(values["parent_id"]) if values["parent_id"] else None
            persisted.update(imported_values=values, snapshot_sha256=sha)
            if is_new:
                self.connection.execute(sa.insert(categories).values(**persisted))
            else:
                self.connection.execute(
                    sa.update(categories)
                    .where(categories.c.id == persisted["id"])
                    .values(**persisted, updated_at=sa.func.now())
                )
        run_id = uuid4()
        self.connection.execute(
            sa.insert(import_runs).values(
                id=run_id,
                snapshot_sha256=sha,
                source=snapshot.source.key,
                commit_sha=commit_sha,
                result=plan.counts(),
                review=review.model_dump(mode="json") if review else sa.null(),
                permission=permission.model_dump(mode="json") if permission else sa.null(),
            )
        )
        return str(run_id)

    def commit(self) -> None:
        """Фиксирует всю операцию только после успешного сохранения audit trail."""
        self.transaction.commit()


def check_database(engine: Engine) -> dict:
    """Проверяет реальное соединение, миграцию и доступность проектных таблиц."""
    with engine.connect() as connection:
        revision = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        if revision != "0006_identity_physical_merge":
            raise TaxonomyError("Версия schema не соответствует приложению")
        count = connection.execute(sa.select(sa.func.count()).select_from(categories)).scalar_one()
    return {"status": "ready", "revision": revision, "categories": count}
