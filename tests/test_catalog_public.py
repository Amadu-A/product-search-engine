# tests/test_catalog_public.py
"""Контракты витрины и счётчики прогресса на настоящей PostgreSQL."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.infrastructure.catalog_store import CatalogStore
from app.presentation.api import create_app, source_url
from tests.test_catalog_pipeline import catalog as catalog
from tests.test_catalog_pipeline import input_file, run_import


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "file:///test",
        "https://user:pass@example.org",
        "https://[",
        "\nhttps://example.org",
    ],
)
def test_catalog_rejects_unsafe_source_link(value):
    """В браузер передаются только исходные HTTP(S)-ссылки без учётных данных."""
    assert source_url(value) is None


@pytest.mark.integration
def test_public_catalog_contract_and_candidate_directories(catalog, tmp_path):
    """Импорт пополняет справочники, а витрина читает ветку, карточку и историческую цену."""
    parent = catalog.create_category(
        {"name": "Синтетическая техника", "reason": "Тест"}, "test", "p"
    )
    leaf = catalog.create_category(
        {"name": "Синтетические кабели", "parent_id": parent["id"], "reason": "Тест"}, "test", "l"
    )
    job = run_import(
        catalog,
        input_file(
            tmp_path,
            [
                {
                    "name": "Кабель первый 220В",
                    "mpn": "KT-01",
                    "brand": "Marca",
                    "manufacturer": "Завод А",
                    "seller": "Магазин Б",
                    "product_url": "https://example.org/archive/1",
                    "price": "123.45",
                    "currency": "RUB",
                    "price_date": "2024-01-10",
                    "unit": "м",
                },
                {
                    "name": "Кабель второй 380В",
                    "mpn": "KT-01",
                    "brand": "marca",
                    "manufacturer": "завод а",
                },
                {"name": "Кабель третий", "product_url": "javascript:alert(1)"},
            ],
        ),
    )
    first, second, third = catalog.report(job)["items"]
    catalog.decide(
        first["id"],
        {"expected_version": first["processing_version"], "category_id": leaf["id"]},
        "test",
        "set-leaf",
    )
    with catalog.store.transaction() as unit:
        assert len(unit.all("brands")) == len(unit.all("manufacturers")) == 1
        assert len({first["product_id"], second["product_id"], third["product_id"]}) == 3
        point = unit.all("price_observations", raw_record_id=UUID(first["raw_record_id"]))[0]
        unit.add(
            "price_history",
            offer_id=point["offer_id"],
            price_observation_id=point["id"],
            amount="123.45",
            currency="RUB",
            sale_unit="м",
            price_type="retail",
            valid_from=datetime(2024, 1, 10, tzinfo=UTC),
            verified_by="fixture",
        )
        unit.add(
            "price_observations",
            offer_id=point["offer_id"],
            raw_record_id=UUID(third["raw_record_id"]),
            raw_price="999",
            amount="999",
            observed_at=datetime(2024, 2, 3, tzinfo=UTC),
            quality_status="unverified",
        )
    client = TestClient(create_app(catalog))
    tree = {c["id"]: c for c in client.get("/api/v1/categories/tree").json()}
    assert tree[parent["id"]]["product_count"] == 0
    assert tree[parent["id"]]["subtree_product_count"] == 1
    manufacturers = client.get("/api/v1/manufacturers").json()
    assert len(manufacturers) == 1 and manufacturers[0]["status"] == "candidate"
    params = {
        "q": "kt01",
        "manufacturer_id": manufacturers[0]["id"],
        "limit": 1,
        "sort": "name_desc",
    }
    found = client.get("/api/v1/products", params=params).json()
    assert found["total"] == 2 and found["items"][0]["id"] == first["product_id"]
    following = client.get("/api/v1/products", params={**params, "offset": 1}).json()
    assert following["items"][0]["id"] == second["product_id"]
    branch = client.get("/api/v1/products", params={"category_id": parent["id"]}).json()
    assert [row["id"] for row in branch["items"]] == [first["product_id"]]
    assert client.get("/api/v1/products?uncategorized=true").json()["total"] == 2
    assert client.get("/api/v1/products?q=%25").json()["total"] == 0
    assert client.get("/api/v1/products?sort=external_sql").status_code == 422
    card = client.get(f"/api/v1/products/{first['product_id']}").json()
    assert card["category_path"] == ["Синтетическая техника", "Синтетические кабели"]
    assert card["source_links"][0]["url"] == "https://example.org/archive/1"
    assert card["manufacturer_status"] == card["brand_status"] == "candidate"
    offers = client.get(f"/api/v1/products/{first['product_id']}/offers").json()
    assert offers["price_policy"] == "historical"
    offer = offers["items"][0]
    assert offer["seller_name"] == "Магазин Б"
    assert offer["history_total"] == 1
    price = offer["price_history"][0]
    assert price["source_reported_date"] == "2024-01-10"
    assert price["verified_amount"] == "123.450000"
    assert "current_price" not in offer and price["currency"] == "RUB"
    next_history = client.get(
        f"/api/v1/products/{first['product_id']}/offers", params={"history_offset": 1}
    ).json()["items"][0]
    assert next_history["history_total"] == 1 and not next_history["price_history"]
    hidden_url = client.get(f"/api/v1/products/{third['product_id']}").json()
    assert not hidden_url["source_links"] and hidden_url["manufacturer_name"] is None
    catalog.archive_product(first["product_id"], "test", "retire")
    assert client.get(f"/api/v1/products/{first['product_id']}/offers").status_code == 404
    assert client.get(f"/api/v1/products/{first['product_id']}").status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize("db_engine", ["0003_catalog_pipeline"], indirect=True)
def test_progress_migration_bulk_retry_rollback_and_delete(db_engine):
    """Baseline старого задания и дельты совпадают при retry, rollback и удалении."""
    store = CatalogStore(db_engine)
    with store.transaction() as unit:
        job = unit.add("ingest_jobs", status="completed")["id"]
        file = unit.add(
            "ingest_job_files",
            job_id=job,
            original_filename="synthetic.json",
            format="json",
            byte_size=1,
            content_sha256="0" * 64,
            storage_key="test",
        )["id"]
        assigned = unit.add(
            "ingest_job_items", job_id=job, file_id=file, item_position=0, status="assigned"
        )["id"]
        unit.add(
            "ingest_job_items", job_id=job, file_id=file, item_position=1, status="taxonomy_gap"
        )
    config = Config("alembic.ini")
    config.attributes["engine"] = db_engine
    command.upgrade(config, "head")
    with store.transaction() as unit:
        assert unit.refresh_counts(job) == {
            "total_count": 2,
            "processed_count": 2,
            "assigned_count": 1,
            "needs_review_count": 0,
            "taxonomy_gap_count": 1,
            "error_count": 0,
        }
        unit.update("ingest_job_items", assigned, status="pending")
        unit.update("ingest_job_items", assigned, status="classifying")
        assert unit.refresh_counts(job)["processed_count"] == 1
    with pytest.raises(RuntimeError, match="Тестовый rollback"), store.transaction() as unit:
        items = unit.table("ingest_job_items")
        unit.connection.execute(items.update().where(items.c.job_id == job).values(status="error"))
        assert unit.refresh_counts(job)["error_count"] == 2
        raise RuntimeError("Тестовый rollback")
    with store.transaction() as unit:
        assert unit.refresh_counts(job)["error_count"] == 0
        items = unit.table("ingest_job_items")
        unit.connection.execute(items.delete().where(items.c.id == assigned))
        assert unit.refresh_counts(job)["total_count"] == 1
