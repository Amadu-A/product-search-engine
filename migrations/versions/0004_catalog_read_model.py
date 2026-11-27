# migrations/versions/0004_catalog_read_model.py
"""Индексы витрины и транзакционные счётчики задания без пересоздания таблиц."""

import sqlalchemy as sa
from alembic import op

revision = "0004_catalog_read_model"
down_revision = "0003_catalog_pipeline"
branch_labels = None
depends_on = None

BUCKETS = {
    "total_count": "true",
    "processed_count": (
        "status IN ('assigned','needs_review','taxonomy_gap',"
        "'not_product','invalid_record','error')"
    ),
    "assigned_count": "status = 'assigned'",
    "needs_review_count": "status = 'needs_review'",
    "taxonomy_gap_count": "status = 'taxonomy_gap'",
    "error_count": "status IN ('invalid_record','error')",
}

CHANGES = {
    "insert": "SELECT job_id, status, 1 AS delta FROM added",
    "update": """
        SELECT prior.job_id, prior.status, -1 AS delta
        FROM previous AS prior JOIN added AS next_row USING (id)
        WHERE (prior.job_id, prior.status) IS DISTINCT FROM (next_row.job_id, next_row.status)
        UNION ALL
        SELECT next_row.job_id, next_row.status, 1 AS delta
        FROM previous AS prior JOIN added AS next_row USING (id)
        WHERE (prior.job_id, prior.status) IS DISTINCT FROM (next_row.job_id, next_row.status)
    """,
    "delete": "SELECT job_id, status, -1 AS delta FROM previous",
}
REFERENCES = {
    "insert": "NEW TABLE AS added",
    "update": "OLD TABLE AS previous NEW TABLE AS added",
    "delete": "OLD TABLE AS previous",
}


def upgrade() -> None:
    """Пересчитывает baseline один раз, затем применяет только изменения статусов."""
    counts = ", ".join(
        f"count(i.id) FILTER (WHERE {condition.replace('status', 'i.status')}) AS {key}"
        for key, condition in BUCKETS.items()
    )
    baseline = ", ".join(f"{key} = counts.{key}" for key in BUCKETS)
    op.execute(sa.text("LOCK TABLE ingest_job_items IN SHARE ROW EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            f"""
            UPDATE ingest_jobs AS job SET {baseline}
            FROM (
                SELECT j.id, {counts}
                FROM ingest_jobs AS j LEFT JOIN ingest_job_items AS i ON i.job_id = j.id
                GROUP BY j.id
            ) AS counts WHERE job.id = counts.id
            """
        )
    )
    for action, changes in CHANGES.items():
        create_progress_trigger(action, changes)
    op.execute(
        sa.text(
            """
            CREATE INDEX ix_observations_raw ON price_observations(raw_record_id);
            CREATE INDEX ix_items_normalized ON ingest_job_items(normalized_record_id);
            CREATE INDEX ix_items_catalog_product ON ingest_job_items(product_id)
                WHERE product_id IS NOT NULL;
            CREATE INDEX ix_items_catalog_raw ON ingest_job_items(raw_record_id, product_id)
                WHERE product_id IS NOT NULL;
            CREATE INDEX ix_catalog_product_name ON products(lower(canonical_name), id)
                WHERE status <> 'retired';
            CREATE INDEX ix_catalog_brand_candidate ON brands(normalized_name)
                WHERE status = 'candidate';
            CREATE INDEX ix_catalog_manufacturer_candidate ON manufacturers(normalized_name)
                WHERE status = 'candidate' AND inn IS NULL AND country_code IS NULL;
            """
        )
    )


def create_progress_trigger(action: str, changes: str) -> None:
    """Применяет один суммарный delta на задание за SQL-оператор."""
    totals = ", ".join(
        f"COALESCE(sum(delta) FILTER (WHERE {condition}), 0) AS {key}"
        for key, condition in BUCKETS.items()
    )
    updates = ", ".join(f"{key} = job.{key} + counts.{key}" for key in BUCKETS)
    changed = " OR ".join(f"counts.{key} <> 0" for key in BUCKETS)
    name = f"ingest_item_progress_{action}"
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                WITH changes AS ({changes}),
                counts AS (SELECT job_id, {totals} FROM changes GROUP BY job_id)
                UPDATE ingest_jobs AS job SET {updates}
                FROM counts WHERE job.id = counts.job_id AND ({changed});
                RETURN NULL;
            END;
            $$;
            CREATE TRIGGER {name} AFTER {action.upper()} ON ingest_job_items
            REFERENCING {REFERENCES[action]} FOR EACH STATEMENT EXECUTE FUNCTION {name}();
            """
        )
    )


def downgrade() -> None:
    """Оставляет откат данных проверенному резервному копированию."""
    raise RuntimeError("Восстановите проверенный backup перед возвратом к предыдущей схеме")
