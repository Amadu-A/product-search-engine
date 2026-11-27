# migrations/versions/0006_identity_physical_merge.py
"""Разрешает хранить перенаправления после удаления поглощённой карточки."""

from alembic import op
from sqlalchemy import text

revision = "0006_identity_physical_merge"
down_revision = "0005_product_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Оставляет FK на живую цель и не изменяет товары при развёртывании."""
    op.drop_constraint(
        "product_merge_redirects_old_product_id_fkey", "product_merge_redirects", type_="foreignkey"
    )
    op.create_index("ix_product_merge_target", "product_merge_redirects", ["product_id"])
    op.create_index("ix_price_observations_raw", "price_observations", ["raw_record_id"])
    op.create_index(
        "ix_product_confirmed_identity",
        "product_identifiers",
        ["identifier_type", "normalized_value", "issuer", "product_id"],
        postgresql_where=text("status = 'confirmed'"),
    )


def downgrade() -> None:
    """Возвращает FK, если все исходные UUID ещё существуют."""
    op.create_foreign_key(
        "product_merge_redirects_old_product_id_fkey",
        "product_merge_redirects",
        "products",
        ["old_product_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_index("ix_product_merge_target", table_name="product_merge_redirects")
    op.drop_index("ix_price_observations_raw", table_name="price_observations")
    op.drop_index("ix_product_confirmed_identity", table_name="product_identifiers")
