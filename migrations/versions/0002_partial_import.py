# migrations/versions/0002_partial_import.py
"""Разрешает аудит без review и добавляет признаки качества без удаления категорий."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_partial_import"
down_revision = "0001_taxonomy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Сохраняет старый аудит и baseline, добавляя только пустой набор новых quality flags."""
    op.alter_column("taxonomy_import_runs", "review", existing_type=JSONB, nullable=True)
    op.add_column(
        "categories",
        sa.Column(
            "quality_flags",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_categories_quality_flags",
        "categories",
        "jsonb_typeof(quality_flags) = 'array'",
    )
    op.execute(
        sa.text("""
        UPDATE categories
        SET imported_values = imported_values || jsonb_build_object('quality_flags', '[]'::jsonb)
        WHERE imported_values IS NOT NULL AND NOT (imported_values ? 'quality_flags')
    """)
    )


def downgrade() -> None:
    """Не удаляет признаки качества или аудит; восстановление выполняется из backup."""
    raise RuntimeError("Destructive downgrade запрещён; используйте проверенный backup")
