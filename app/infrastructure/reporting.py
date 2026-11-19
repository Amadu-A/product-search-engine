# app/infrastructure/reporting.py
"""SQL-проверки реального дерева, включая недостижимые циклы и историю импортов."""

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.infrastructure.database import categories, import_runs, snapshots

TREE_SQL = """
WITH RECURSIVE tree AS (
    SELECT id, parent_id, id AS root_id, name AS root_name, 1 AS level
    FROM categories WHERE parent_id IS NULL
    UNION ALL
    SELECT c.id, c.parent_id, t.root_id, t.root_name, t.level + 1
    FROM categories c JOIN tree t ON c.parent_id = t.id
)
SELECT level, root_id::text, root_name, count(*) AS count
FROM tree GROUP BY level, root_id, root_name ORDER BY root_name, level
"""
COUNTS_SQL = """
SELECT
    count(*) AS total,
    count(*) FILTER (WHERE parent_id IS NULL) AS roots,
    count(*) FILTER (WHERE NOT EXISTS (
        SELECT 1 FROM categories child WHERE child.parent_id = c.id
    )) AS leaves,
    count(*) FILTER (WHERE parent_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM categories parent WHERE parent.id = c.parent_id
    )) AS orphans,
    count(*) FILTER (WHERE source = 'custom') AS custom,
    count(*) FILTER (WHERE length(trim(name)) = 0) AS empty_names
FROM categories c
"""
CYCLES_SQL = """
WITH RECURSIVE ancestry AS (
    SELECT id, parent_id, ARRAY[id] AS path, false AS cycle FROM categories
    UNION ALL
    SELECT p.id, p.parent_id, a.path || p.id, p.id = ANY(a.path)
    FROM ancestry a JOIN categories p ON p.id = a.parent_id WHERE NOT a.cycle
), cycle_keys AS (
    SELECT (SELECT string_agg(item::text, ',' ORDER BY item)
            FROM unnest(path[array_position(path, id) : (cardinality(path)-1)]) item) AS key
    FROM ancestry WHERE cycle
)
SELECT count(DISTINCT key) AS cycles FROM cycle_keys
"""
DUPLICATES_SQL = """
SELECT coalesce(sum(n - 1), 0)::int AS duplicates FROM (
    SELECT count(*) n FROM categories WHERE external_id IS NOT NULL
    GROUP BY source, external_id HAVING count(*) > 1
) duplicates
"""


class SqlReportRepository:
    """Читает согласованный repeatable-read отчёт без изменения данных."""

    def __init__(self, engine: Engine) -> None:
        """Принимает проверенный project engine из composition root."""
        self.engine = engine

    def inspect(self) -> dict:
        """Считает метрики SQL и возвращает реальные результаты последних двадцати apply."""
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            with conn.begin():
                conn.execute(sa.text("SET TRANSACTION READ ONLY"))
                counts = dict(conn.execute(sa.text(COUNTS_SQL)).mappings().one())
                structure = [dict(r) for r in conn.execute(sa.text(TREE_SQL)).mappings()]
                counts["cycles"] = conn.execute(sa.text(CYCLES_SQL)).scalar_one()
                counts["duplicates"] = conn.execute(sa.text(DUPLICATES_SQL)).scalar_one()
                counts["reachable"] = sum(row["count"] for row in structure)
                counts["unreachable"] = counts["total"] - counts["reachable"]
                counts["max_depth"] = max((r["level"] for r in structure), default=0)
                counts["errors"] = sum(
                    counts[k]
                    for k in ("cycles", "duplicates", "orphans", "unreachable", "empty_names")
                )
                quality_nodes = [
                    dict(row)
                    for row in conn.execute(
                        sa.select(
                            categories.c.id,
                            categories.c.external_id,
                            categories.c.name,
                            categories.c.source,
                            categories.c.quality_flags,
                        )
                        .where(categories.c.quality_flags != [])
                        .order_by(categories.c.source, categories.c.external_id)
                    ).mappings()
                ]
                counts["suspected_categories"] = len(quality_nodes)
                sources = [
                    dict(r)
                    for r in conn.execute(
                        sa.text(
                            "SELECT source, count(*) AS count FROM categories "
                            "GROUP BY source ORDER BY source"
                        )
                    ).mappings()
                ]
                history = [
                    dict(r)
                    for r in conn.execute(
                        sa.select(import_runs).order_by(import_runs.c.created_at.desc()).limit(20)
                    ).mappings()
                ]
                used = {r["snapshot_sha256"] for r in history}
                saved = [
                    dict(r)
                    for r in conn.execute(
                        sa.select(snapshots).where(snapshots.c.sha256.in_(used))
                    ).mappings()
                ]
                return {
                    "counts": counts,
                    "structure": structure,
                    "sources": sources,
                    "history": history,
                    "snapshots": saved,
                    "quality_nodes": quality_nodes,
                }
