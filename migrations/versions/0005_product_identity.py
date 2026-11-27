# migrations/versions/0005_product_identity.py
"""Добавляет индекс доказуемых ключей и аудит переноса Product."""

import sqlalchemy as sa
from alembic import op

revision = "0005_product_identity"
down_revision = "0004_catalog_read_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Сохраняет существующие UUID и данные, создаёт пустые таблицы сопоставления."""
    op.execute(
        sa.text("""
        CREATE TABLE product_identity_keys (
            identity_key text NOT NULL,
            product_id uuid NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
            PRIMARY KEY (identity_key, product_id)
        );
        CREATE INDEX ix_product_identity_product ON product_identity_keys(product_id);
        CREATE TABLE product_merge_redirects (
            old_product_id uuid PRIMARY KEY REFERENCES products(id) ON DELETE RESTRICT,
            product_id uuid NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
            evidence jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (old_product_id <> product_id)
        );
    """)
    )


def downgrade() -> None:
    """Оставляет откат объединений проверенной резервной копии."""
    raise RuntimeError("Используйте проверенную резервную копию перед возвратом прежней схемы")
