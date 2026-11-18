# migrations/versions/0001_taxonomy.py
"""Начальная project-specific схема категорий, снимков и аудита; существующие данные не удаляет."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_taxonomy"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создаёт дерево с UUID, unique source/key и baseline защиты ручных изменений."""
    op.create_table(
        "taxonomy_snapshots",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("parent_id", sa.Uuid, sa.ForeignKey("categories.id", ondelete="RESTRICT")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.String(160), nullable=False, unique=True),
        sa.Column("sort_order", sa.Integer, nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("external_id", sa.Text),
        sa.Column("source_url", sa.Text),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
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
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id", name="ck_categories_self_parent"
        ),
        sa.CheckConstraint(
            "source = 'custom' OR external_id IS NOT NULL", name="ck_categories_key"
        ),
    )
    op.create_index("ix_categories_parent_id", "categories", ["parent_id"])
    op.create_table(
        "taxonomy_import_runs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "snapshot_sha256",
            sa.String(64),
            sa.ForeignKey("taxonomy_snapshots.sha256"),
            nullable=False,
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("review", JSONB, nullable=False),
        sa.Column("permission", JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    """Запрещает автоматическое удаление категорий; откат выполняется восстановлением backup."""
    raise RuntimeError("Удаление taxonomy schema запрещено; используйте проверенный backup")
