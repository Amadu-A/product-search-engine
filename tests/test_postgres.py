# tests/test_postgres.py
"""Live integration: настоящие миграции, rollback, custom, repeat и SQL-отчёт в тестовой БД."""

import json
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
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
from app.infrastructure.files import normalize_file, write_json
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
        "revision": "0006_identity_physical_merge",
        "categories": 0,
    }
    with db_engine.connect() as conn:
        differences = compare_metadata(
            MigrationContext.configure(
                conn,
                opts={
                    "include_object": lambda obj, name, kind, reflected, compare_to: (
                        name in metadata.tables if kind == "table" else True
                    ),
                },
            ),
            metadata,
        )
    assert differences == []


@pytest.mark.parametrize("db_engine", ["0005_product_identity"], indirect=True)
def test_identity_upgrade_preserves_legacy_redirect_rows(db_engine):
    """Обновляет заполненную 0005 и сохраняет FK на цель при удалении исходного UUID."""
    old, target = uuid4(), uuid4()
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO products (id, canonical_name, status) VALUES "
                "(:old, 'Старый дубликат', 'retired'), (:target, 'Товар', 'provisional')"
            ),
            {"old": old, "target": target},
        )
        conn.execute(
            sa.text(
                "INSERT INTO product_merge_redirects (old_product_id,product_id,evidence) "
                "VALUES (:old,:target,'{}')"
            ),
            {"old": old, "target": target},
        )
    config = Config("alembic.ini")
    config.attributes["engine"] = db_engine
    command.upgrade(config, "head")
    with db_engine.begin() as conn:
        assert conn.scalar(sa.text("SELECT count(*) FROM products")) == 2
        conn.execute(sa.text("DELETE FROM products WHERE id=:old"), {"old": old})
        assert conn.scalar(sa.text("SELECT product_id FROM product_merge_redirects")) == target
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(sa.text("DELETE FROM products WHERE id=:target"), {"target": target})


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


def test_partial_apply_repeat_extend_and_report(
    db_engine, partial_input, partial_snapshot, tmp_path
):
    """Неполное дерево дополняется без дубликатов, подтверждений или потери custom."""
    first = importer(db_engine).execute(partial_snapshot, apply=True)
    assert first["added"] == 8 and first["metrics"]["complete"] is False
    path, collector_report = partial_input
    repeated_snapshot = normalize_file(
        path, tmp_path / "repeated", 8388608, collector_report_path=collector_report
    )
    repeated = importer(db_engine).execute(repeated_snapshot, apply=True)
    assert repeated["added"] == repeated["updated"] == 0 and repeated["skipped"] == 8
    custom_id = uuid4()
    with db_engine.begin() as conn:
        conn.execute(
            sa.insert(categories).values(
                id=custom_id,
                name="Пользовательская",
                slug="own-custom",
                sort_order=0,
                source="custom",
            )
        )
    data = json.loads(path.read_bytes())
    data["nodes"][0]["children"].append({"external_id": "new-child", "name": "Новая дочерняя"})
    data["nodes"][1]["children"][0]["name"] = "Трубы пластиковые"
    write_json(path, data)
    extended = normalize_file(
        path, tmp_path / "extended", 8388608, collector_report_path=collector_report
    )
    result = importer(db_engine).execute(extended, apply=True)
    assert result["added"] == 1 and result["updated"] == 1 and result["after"] == 10
    with db_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(categories.c.name).where(categories.c.id == custom_id)
            ).scalar_one()
            == "Пользовательская"
        )
        stored = (
            conn.execute(sa.select(import_runs).order_by(import_runs.c.created_at)).mappings().all()
        )
        assert all(row["review"] is None and row["permission"] is None for row in stored)
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(import_runs)
                .where(import_runs.c.review.is_(None), import_runs.c.permission.is_(None))
            ).scalar_one()
            == 3
        )
        assert stored[0]["result"]["metrics"]["collection"]["errors"] == 20
        assert stored[0]["result"]["metrics"]["collection"]["redirects"] == 4
        assert stored[0]["result"]["metrics"]["source_information"]["license"] == "UNKNOWN"
    report = BuildReport(SqlReportRepository(db_engine)).execute("test-sha", {})
    assert '"complete": false' in report and '"remaining_pages": 192' in report
    assert '"repeated_unchanged": true' in report


def test_partial_quality_flags_are_persisted(db_engine, partial_input, tmp_path):
    """Подозрительная коллекция импортируется с исходной ценой и явными flags для проверки."""
    path, collector_report = partial_input
    data = json.loads(path.read_bytes())
    data["nodes"][0]["children"][0]["children"][0]["name"] = "Серия (Тест)от 500 ₽ / м²"
    write_json(path, data)
    snapshot = normalize_file(
        path, tmp_path / "quality", 8388608, collector_report_path=collector_report
    )
    importer(db_engine).execute(snapshot, apply=True)
    report = SqlReportRepository(db_engine).inspect()
    assert report["counts"]["total"] == 8 and report["counts"]["errors"] == 0
    assert report["counts"]["suspected_categories"] == 1
    row = report["quality_nodes"][0]
    assert row["name"] == "Серия (Тест)от 500 ₽ / м²"
    assert row["quality_flags"] == ["price_in_name", "suspected_product_collection"]


@pytest.mark.parametrize("db_engine", ["0001_taxonomy"], indirect=True)
def test_upgrade_preserves_old_baseline_and_manual_name(db_engine, snapshot):
    """Миграция старой БД не перезаписывает ручные значения и не создаёт ложных конфликтов."""
    old_categories = sa.Table("categories", sa.MetaData(), autoload_with=db_engine)
    values = snapshot.nodes[0].imported_values()
    values.pop("quality_flags")
    persisted = {**values, "id": UUID(values["id"]), "imported_values": values}
    persisted["name"] = "Ручное имя до миграции"
    with db_engine.begin() as conn:
        conn.execute(sa.insert(old_categories).values(**persisted))
    config = Config("alembic.ini")
    config.attributes["engine"] = db_engine
    command.upgrade(config, "head")
    with db_engine.connect() as conn:
        row = conn.execute(sa.select(categories)).mappings().one()
        assert row["name"] == "Ручное имя до миграции" and row["quality_flags"] == []
        assert row["imported_values"] == {**values, "quality_flags": []}
    with pytest.raises(TaxonomyError, match="Ручные"):
        importer(db_engine).execute(snapshot, apply=True)
    with db_engine.begin() as conn:
        conn.execute(sa.update(categories).values(name=values["name"]))
    result = importer(db_engine).execute(snapshot, apply=True)
    assert result["added"] == 7 and result["skipped"] == 1
