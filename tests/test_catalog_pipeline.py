# tests/test_catalog_pipeline.py
"""Integration/regression: настоящая PostgreSQL и Qdrant, mock inference выделен явно."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.catalog_bootstrap import build_catalog
from app.domain.catalog import ConflictError, DependencyUnavailable
from app.presentation.api import create_app
from app.settings import Settings
from tests.test_postgres import importer

pytestmark = pytest.mark.integration


@pytest.fixture
def catalog(db_engine, snapshot, tmp_path):
    """Использует уникальную SQL schema и собственный alias временного Qdrant."""
    importer(db_engine).execute(snapshot, apply=True)
    settings = Settings(
        inference_mode="mock",
        job_transport="postgres",
        upload_dir=tmp_path / "uploads",
        qdrant_collection_alias="product_taxonomy_categories_test_" + uuid4().hex,
    )
    service = build_catalog(settings, db_engine)
    yield service
    state = service.index.status()
    if state.get("collection_name"):
        service.index.request(
            "POST",
            "/collections/aliases",
            {"actions": [{"delete_alias": {"alias_name": service.index.alias}}]},
        )
        service.index.request("DELETE", "/collections/" + state["collection_name"])


def input_file(tmp_path, rows=None) -> Path:
    """Записывает только собственные синтетические товары, без внешнего каталога."""
    path = tmp_path / "input.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "synthetic_test",
                "items": rows
                or [
                    {
                        "source_key": "one",
                        "name": "Кабель силовой тестовый",
                        "price": "12.50",
                        "price_date": "2024-11-30",
                    },
                    {"source_key": "two", "name": "Каталог коллекция кабелей"},
                    {"source_key": "three", "name": "MPN123"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def run_import(service, path):
    """Запускает один общий use-case и реальный PG claim без внешней очереди."""
    preview = service.upload([(path.name, path)])
    identifier = preview["job"]["id"]
    service.start(identifier, "start:" + identifier)
    assert service.store.claim_job(identifier) == identifier
    service.process(identifier)
    return identifier


def test_migration_preserves_categories_and_raw_immutable(catalog):
    """Schema содержит новые таблицы, старые UUID сохранены; raw нельзя переписать."""
    assert len(catalog.store.categories()) == 8
    assert set(catalog.store.tables) >= {
        "price_observations",
        "product_understandings",
        "ingest_job_source_runs",
        "taxonomy_index_outbox",
    }
    with pytest.raises(RuntimeError):
        with catalog.store.transaction() as unit:
            unit.add("sources", name="rollback", kind="other")
            raise RuntimeError("synthetic rollback")
    with catalog.store.transaction() as unit:
        assert not unit.all("sources", name="rollback")


def test_reimport_manual_priority_and_observations(catalog, tmp_path):
    """Повтор не плодит raw/Product/наблюдения; ручная категория имеет приоритет."""
    path = input_file(tmp_path)
    first = run_import(catalog, path)
    with catalog.store.transaction() as unit:
        items = sorted(
            unit.all("ingest_job_items", job_id=UUID(first)), key=lambda i: i["item_position"]
        )
        assert [i["status"] for i in items] == ["needs_review", "not_product", "needs_review"]
        assert len(unit.all("products")) == 1
        assert len(unit.all("price_observations")) == 1
        assert not unit.all("price_history")
        raw = unit.get("raw_records", items[0]["raw_record_id"])
    with pytest.raises(sa.exc.DBAPIError):
        with catalog.store.transaction() as unit:
            unit.update("raw_records", raw["id"], payload={"name": "mutated"})
    category = catalog.store.categories()[0]
    decision = {
        "category_id": str(category["id"]),
        "expected_version": 1,
        "reason": "Синтетическая проверка",
    }
    assigned = catalog.decide(items[0]["id"], decision, "test_operator", "manual-one")
    assert assigned["status"] == "assigned"
    assert catalog.decide(items[0]["id"], decision, "test_operator", "manual-one") == assigned
    with pytest.raises(ConflictError):
        catalog.decide(items[0]["id"], decision, "test_operator", "manual-two")
    repeated = run_import(catalog, path)
    with catalog.store.transaction() as unit:
        second = sorted(
            unit.all("ingest_job_items", job_id=UUID(repeated)), key=lambda i: i["item_position"]
        )
        assert second[0]["status"] == "assigned" and second[0]["category_id"] == category["id"]
        assert len(unit.all("raw_records")) == 3
        assert len(unit.all("products")) == 1
        assert len(unit.all("price_observations")) == 1
        assert len(unit.all("source_runs")) == 2
    report = catalog.report(repeated)
    assert report["database_counts"]["raw_records"] == 3
    assert report["duplicate_key_groups"] == {
        "category_slugs": 0,
        "category_source_ids": 0,
        "raw_records": 0,
        "normalized_records": 0,
        "offers": 0,
        "price_observations": 0,
    }


def test_qdrant_outbox_reconciliation_and_stale_filter(catalog):
    """Реальный Qdrant подтверждает UUID/hash; изменения родителя переиндексируют пути."""
    result = catalog.index.sync()
    assert result["status"] == "ready" and result["points"] == 8
    assert catalog.index.sync()["upserted"] == 0
    rows = catalog.store.categories()
    root = next(r for r in rows if r["parent_id"] is None)
    with catalog.store.transaction() as unit:
        unit.update("categories", root["id"], name="Корень после ручного изменения")
    assert catalog.index.status()["pending_events"] == 1
    result = catalog.index.sync()
    points = catalog.index.scroll(result["collection_name"])
    for row in catalog.store.categories():
        if str(root["id"]) in row["path_ids"]:
            assert "Корень после ручного изменения" in points[str(row["id"])]["path_names"]
    with catalog.store.transaction() as unit:
        unit.update("categories", root["id"], is_active=False)
    result = catalog.index.sync()
    assert str(root["id"]) not in catalog.index.scroll(result["collection_name"])
    candidates, warnings = catalog.index.candidates(
        {"product_type": "Кабель", "applications": [], "attributes": []}
    )
    assert not warnings
    assert all(str(root["id"]) not in c["path_ids"] for c in candidates)


def test_api_mapping_idempotency_cancel_and_sse(catalog, tmp_path):
    """HTTP использует общий start, replay SSE и не принимает start с неподтверждённым mapping."""
    client = TestClient(create_app(catalog))
    response = client.post(
        "/api/v1/import-jobs",
        files={"files": ("input.json", b'[{"Description":"Cable test"}]', "application/json")},
    )
    assert response.status_code == 201
    identifier = response.json()["job"]["id"]
    assert not response.json()["can_start"]
    assert (
        client.post(
            f"/api/v1/import-jobs/{identifier}/start", headers={"Idempotency-Key": "start-failed"}
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/api/v1/import-jobs/{identifier}/mapping", json={"fields": {"name": "Description"}}
        ).status_code
        == 200
    )
    first = client.post(
        f"/api/v1/import-jobs/{identifier}/start", headers={"Idempotency-Key": "start-ok"}
    )
    assert first.status_code == 202
    assert (
        client.post(
            f"/api/v1/import-jobs/{identifier}/start", headers={"Idempotency-Key": "start-ok"}
        ).json()
        == first.json()
    )
    assert client.post(f"/api/v1/import-jobs/{identifier}/cancel").status_code == 202
    with catalog.store.transaction() as unit:
        events = unit.events(identifier, 0)
    replay = client.get(
        f"/api/v1/import-jobs/{identifier}/events",
        headers={"Last-Event-ID": str(events[-2]["event_id"])},
    )
    assert str(events[-1]["event_id"]) in replay.text
    assert f"id: {events[0]['event_id']}\n" not in replay.text
    assert client.get("/admin/import").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_new_category_duplicate_and_sync_pending(catalog):
    """Создание SQL+outbox атомарно; sibling-дубликат не проходит, Qdrant ACK отдельный."""
    values = {"name": "Собственная тестовая категория", "reason": "Тест", "is_assignable": True}
    category = catalog.create_category(values, "test", "new-one")
    assert category["sync_status"] == "sync_pending"
    assert catalog.create_category(values, "test", "new-one") == category
    with pytest.raises(ConflictError):
        catalog.create_category(values, "test", "new-two")
    assert catalog.index.sync()["points"] == 9


def test_confirmed_identifiers_skip_vlm_and_variant_conflicts(catalog, tmp_path, monkeypatch):
    """Fast lookup требует issuer+confirmed MPN, а конфликт единицы запрещает auto-merge."""
    category = catalog.store.categories()[0]
    with catalog.store.transaction() as unit:
        product = unit.add(
            "products",
            canonical_name="Подтверждённый кабель",
            category_id=category["id"],
            status="identified",
            attributes={"identity_fields": {"unit": "м", "quantity": "2"}},
        )
        unit.add(
            "product_identifiers",
            product_id=product["id"],
            identifier_type="MPN",
            issuer="testbrand",
            raw_value="ABC-1",
            normalized_value="abc1",
            status="confirmed",
        )
        assert unit.confirmed_product({"mpn": "ABC-1", "brand": "Other"}) is None
        assert unit.confirmed_product({"mpn": "ABC-1", "brand": "TestBrand", "unit": "шт"}) is None
        assert unit.confirmed_product({"source_key": "ABC-1", "brand": "TestBrand"}) is None

    def unexpected(step, values):
        """Падает, если fast identity ошибочно вызовет inference."""
        raise AssertionError("VLM не нужна для подтверждённого Product")

    monkeypatch.setattr(catalog.inference, "answer", unexpected)
    identifier = run_import(
        catalog,
        input_file(
            tmp_path,
            [
                {
                    "name": "Кабель с известным MPN",
                    "brand": "TestBrand",
                    "mpn": "ABC-1",
                    "unit": "м",
                    "quantity": "2",
                }
            ],
        ),
    )
    report = catalog.report(identifier)
    assert report["items"][0]["status"] == "assigned"
    assert report["items"][0]["product_id"] == str(product["id"])


def test_unavailable_inference_raw_survives_and_resume(catalog, tmp_path, monkeypatch):
    """Сбой модели не теряет raw/наблюдение и не подменяется успешным mock-ответом."""
    original = catalog.inference.answer

    def unavailable(step, values):
        """Имитирует техническую недоступность внешнего inference."""
        raise DependencyUnavailable("Синтетический timeout")

    monkeypatch.setattr(catalog.inference, "answer", unavailable)
    identifier = run_import(
        catalog, input_file(tmp_path, [{"name": "Кабель тестовый", "price": "12.50"}])
    )
    with catalog.store.transaction() as unit:
        assert len(unit.all("raw_records")) == len(unit.all("price_observations")) == 1
        assert unit.all("product_understandings")[0]["status"] == "unavailable"
        assert not unit.all("products")
    monkeypatch.setattr(catalog.inference, "answer", original)
    catalog.control(identifier, "resume")
    assert catalog.store.claim_job(identifier)
    catalog.process(identifier)
    with catalog.store.transaction() as unit:
        assert unit.all("product_understandings")[0]["status"] == "recognized"
        assert len(unit.all("raw_records")) == len(unit.all("products")) == 1


def test_approved_mapping_precedes_vlm_selection(catalog, tmp_path, monkeypatch):
    """Только утверждённый source mapping назначает категорию без answer_2."""
    category = catalog.store.categories()[0]
    with catalog.store.transaction() as unit:
        source = unit.add("sources", name="synthetic_test", kind="price_file")
        unit.add(
            "category_source_mappings",
            source_id=source["id"],
            external_category_key="Каталог > Кабели",
            category_id=category["id"],
            status="approved",
            approved_by="test",
        )
    original = catalog.inference.answer

    def first_only(step, values):
        """Разрешает answer_1 и доказывает отсутствие лишнего answer_2."""
        assert step == 1
        return original(step, values)

    monkeypatch.setattr(catalog.inference, "answer", first_only)
    identifier = run_import(
        catalog,
        input_file(tmp_path, [{"name": "Кабель тестовый", "category_path": ["Каталог", "Кабели"]}]),
    )
    assert catalog.report(identifier)["items"][0]["status"] == "assigned"


def test_failed_qdrant_has_no_ack_and_recovers(catalog, monkeypatch):
    """Outbox остаётся pending при HTTP-сбое и восстанавливается после reconciliation."""
    original = catalog.index.request

    def unavailable(*args, **kwargs):
        """Имитирует недоступность Qdrant, не меняя PostgreSQL."""
        raise DependencyUnavailable("Синтетический Qdrant outage")

    monkeypatch.setattr(catalog.index, "request", unavailable)
    with pytest.raises(DependencyUnavailable):
        catalog.index.sync()
    with catalog.store.transaction() as unit:
        events = unit.all("taxonomy_index_outbox")
        assert all(e["processed_at"] is None for e in events)
        assert all(e["attempts"] == 1 and e["last_error"] and e["next_attempt_at"] for e in events)
    assert catalog.index.status()["status"] == "error"
    monkeypatch.setattr(catalog.index, "request", original)
    assert catalog.index.sync()["status"] == "ready"


def test_bulk_only_selected_and_manual_not_product(catalog, tmp_path):
    """Пакет требует preview; невыбранные строки и ручное исключение сохраняются."""
    identifier = run_import(catalog, input_file(tmp_path))
    with catalog.store.transaction() as unit:
        items = sorted(
            unit.all("ingest_job_items", job_id=UUID(identifier)), key=lambda r: r["item_position"]
        )
    category_id = str(catalog.store.categories()[0]["id"])
    values = {
        "items": [{"id": str(items[0]["id"]), "expected_version": 1}],
        "category_id": category_id,
        "reason": "Пакет проверен",
    }
    preview = catalog.bulk_preview(values)
    assert len(preview["items"]) == 1
    result = catalog.bulk_apply(
        {**values, "preview_sha256": preview["preview_sha256"]}, "test", "bulk-one"
    )
    assert result["applied"] == [str(items[0]["id"])] and not result["conflicts"]
    assert (
        catalog.bulk_apply(
            {**values, "preview_sha256": preview["preview_sha256"]}, "test", "bulk-one"
        )
        == result
    )
    with catalog.store.transaction() as unit:
        assert unit.get("ingest_job_items", items[1]["id"])["status"] == "not_product"
    catalog.decide(
        items[0]["id"],
        {"action": "not_product", "expected_version": 2, "reason": "Ручное исключение"},
        "test",
        "not-product",
    )
    repeated = run_import(catalog, input_file(tmp_path))
    with catalog.store.transaction() as unit:
        item = next(
            r
            for r in unit.all("ingest_job_items", job_id=UUID(repeated))
            if r["item_position"] == 0
        )
        assert item["status"] == "not_product" and item["product_id"] is None
