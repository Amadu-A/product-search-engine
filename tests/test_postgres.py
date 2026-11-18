# tests/test_postgres.py
"""Live integration: настоящие миграции, rollback, custom, repeat и SQL-отчёт в тестовой БД."""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.application.import_tree import ImportTree
from app.application.report import BuildReport
from app.domain.taxonomy import TaxonomyError
from app.infrastructure.database import (
    SqlImportUnitOfWork,
    categories,
    check_database,
    import_runs,
    metadata,
    snapshots,
)
from app.infrastructure.reporting import SqlReportRepository
from tests.conftest import approved_review

pytestmark = pytest.mark.integration


def importer(engine, *, readonly=False):
    """Собирает тот же production use-case, меняя только тестовое подключение."""
    return ImportTree(SqlImportUnitOfWork(engine, readonly=readonly), 20000, 32)


def test_migration_matches_models(db_engine):
    """Живая Alembic schema точно соответствует ORM metadata и readiness-контракту."""
    assert check_database(db_engine) == {
        "status": "ready",
        "revision": "0001_taxonomy",
        "categories": 0,
    }
    with db_engine.connect() as conn:
        differences = compare_metadata(MigrationContext.configure(conn), metadata)
    assert differences == []


def test_preview_apply_repeat_custom_and_report(db_engine, snapshot, review):
    """Dry-run ничего не пишет; два apply оставляют категории и custom неизменными."""
    preview = importer(db_engine, readonly=True).execute(snapshot)
    assert preview["added"] == 8
    with db_engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(snapshots)).scalar_one() == 0
    first = importer(db_engine).execute(snapshot, apply=True, review=review)
    assert (first["before"], first["after"], first["added"]) == (0, 8, 8)
    custom_id = uuid4()
    with db_engine.begin() as conn:
        conn.execute(
            sa.insert(categories).values(
                id=custom_id,
                name="Моя категория",
                slug="my-category",
                sort_order=0,
                source="custom",
                parent_id=snapshot.nodes[0].id,
            )
        )
        before = list(conn.execute(sa.select(categories).order_by(categories.c.id)).mappings())
    repeated = importer(db_engine).execute(snapshot, apply=True, review=review)
    assert repeated["added"] == repeated["updated"] == 0
    assert repeated["skipped"] == 8 and repeated["after"] == 9
    with db_engine.connect() as conn:
        assert (
            list(conn.execute(sa.select(categories).order_by(categories.c.id)).mappings()) == before
        )
        assert conn.execute(sa.select(sa.func.count()).select_from(import_runs)).scalar_one() == 2
    repository = SqlReportRepository(db_engine)
    data = repository.inspect()
    assert data["counts"]["total"] == 9
    assert data["counts"]["custom"] == 1 and data["counts"]["cycles"] == 0
    assert data["counts"]["errors"] == 0
    report = BuildReport(repository).execute("test-sha", {"tests": "integration"})
    assert '"repeated_unchanged": true' in report
    assert "CC0-1.0" in report and "**IMPORTED**" in report


def test_manual_edit_blocks_and_rolls_back(db_engine, snapshot, review):
    """Изменение названия вручную не перезаписывается, новый audit run не создаётся."""
    importer(db_engine).execute(snapshot, apply=True, review=review)
    with db_engine.begin() as conn:
        conn.execute(
            sa.update(categories)
            .where(categories.c.id == snapshot.nodes[0].id)
            .values(name="Ручное имя")
        )
    with pytest.raises(TaxonomyError, match="Ручные"):
        importer(db_engine).execute(snapshot, apply=True, review=review)
    with db_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(categories.c.name).where(categories.c.id == snapshot.nodes[0].id)
            ).scalar_one()
            == "Ручное имя"
        )
        assert conn.execute(sa.select(sa.func.count()).select_from(import_runs)).scalar_one() == 1


def test_failure_after_writes_rolls_back_everything(db_engine, snapshot, review):
    """Сбой после записи строк и audit не оставляет ни категории, ни снимка, ни run."""

    class FailingUow(SqlImportUnitOfWork):
        """Внедряет отказ в самом конце реальной транзакции."""

        def persist(self, *args, **kwargs):
            """Сначала выполняет SQL, затем имитирует потерю следующей обязательной операции."""
            super().persist(*args, **kwargs)
            raise RuntimeError("Синтетический отказ")

    with pytest.raises(RuntimeError, match="Синтетический"):
        ImportTree(FailingUow(db_engine), 20000, 32).execute(snapshot, apply=True, review=review)
    with db_engine.connect() as conn:
        for table in (categories, snapshots, import_runs):
            assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0


def test_missing_source_nodes_are_preserved(db_engine, snapshot, review):
    """Уменьшение утверждённого источника не удаляет ранее загруженные категории."""
    importer(db_engine).execute(snapshot, apply=True, review=review)
    reduced = snapshot.model_copy(
        update={"nodes": tuple(n for n in snapshot.nodes if n.external_id != "plaster")}
    )
    result = importer(db_engine).execute(reduced, apply=True, review=approved_review(reduced))
    assert result["retained"] == 1 and result["after"] == 8
    with db_engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(categories)).scalar_one() == 8


def test_sql_report_finds_disconnected_cycle(db_engine, snapshot, review):
    """SQL обнаруживает цикл вне корней, который простой обход от корня не увидел бы."""
    importer(db_engine).execute(snapshot, apply=True, review=review)
    with db_engine.begin() as conn:
        conn.execute(
            sa.update(categories)
            .where(categories.c.id == snapshot.nodes[0].id)
            .values(parent_id=snapshot.nodes[1].id)
        )
    counts = SqlReportRepository(db_engine).inspect()["counts"]
    assert counts["cycles"] == 1
    assert counts["unreachable"] == 5 and counts["errors"] > 0
