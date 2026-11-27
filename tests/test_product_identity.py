# tests/test_product_identity.py
"""Регрессии доказательств идентичности, параллельного импорта и объединения истории."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.application.product_merge import ProductMerge, plan_products
from app.domain.catalog import ConflictError
from app.domain.product_identity import (
    claim_value,
    identity_conflicts,
    identity_key,
    identity_profile,
)
from app.presentation.api import create_app
from tests.test_catalog_pipeline import catalog as catalog
from tests.test_catalog_pipeline import input_file, run_import


def legacy_answer(brand, mpn):
    """Имитирует сохранённые дословные признаки первого контракта."""
    return {
        "status": "recognized",
        "record_kind": "product_candidate",
        "product_type": "Светильник",
        "attributes": [],
        "brand_candidate": brand,
        "mpn_candidate": mpn,
    }


def test_elm025_requires_literal_brand_and_manufacturer_code():
    """Указанные EKF и ELM025 доказывают ключ, пропущенные роли требуют проверки."""
    first = identity_profile(
        {"name": "Светильник EKF ELM025 220В 5Вт магнит"}, legacy_answer("EKF", "ELM025")
    )
    equivalent = identity_profile(
        {"name": "Светильник 220 V 5 W магнитный ELM025 EKF"}, legacy_answer("ekf", "elm025")
    )
    different = identity_profile(
        {"name": "Светильник ELM025 EKF 220В 10Вт магнит"}, legacy_answer("EKF", "ELM025")
    )
    assert identity_key(first) == identity_key(equivalent)
    assert identity_conflicts(first, equivalent) == []
    assert identity_conflicts(first, different) == ["power"]
    assert (
        identity_key(
            identity_profile({"name": "Светильник (ELM025)"}, legacy_answer("EKF", "ELM025"))
        )
        is None
    )
    assert (
        identity_key(
            identity_profile({"name": "Светильник EKF PROxima"}, legacy_answer("EKF", "ELM025"))
        )
        is None
    )
    seller_code = identity_profile(
        {"name": "Светильник EKF, SKU ELM025"}, legacy_answer("EKF", "ELM025")
    )
    assert not claim_value(seller_code, "mpn")
    inferred = legacy_answer("EKF", "ELM025") | {
        "identifiers": {
            "brand": {"value": "EKF", "origin": "source", "evidence_ids": ["raw_name"]},
            "mpn": {"value": "ELM025", "origin": "inferred", "evidence_ids": []},
        },
    }
    assert identity_key(identity_profile({"name": "Светильник EKF ELM025"}, inferred)) is None
    assert (
        identity_key(
            identity_profile({"name": "Светильник EKF ELM025.2"}, legacy_answer("EKF", "ELM025"))
        )
        is None
    )
    mislabeled = legacy_answer("EKF", "ELM025") | {"manufacturer_candidate": "EKF"}
    assert not claim_value(
        identity_profile({"name": "Светильник EKF ELM025"}, mislabeled), "manufacturer"
    )


def test_full_cable_variant_and_same_code_of_different_brands():
    """Марка, число жил, сечение и напряжение защищают разные исполнения."""

    def key(name, brand="Завод"):
        """Создаёт ключ кабеля из явно заполненных сведений."""
        return identity_key(identity_profile({"name": name, "brand": brand}))

    base = key("Кабель ВВГнг(А)-LS 3х2,5 0,66кВ")
    assert base and base == key("Кабель ВВГнг(А)-LS 3×2.5 660В")
    assert base != key("Кабель ВВГнг(А)-LS 3×2.5 1000В")
    assert base != key("Кабель ВВГнг(А)-FRLS 3×2.5 660В")
    assert base != key("Кабель ВВГнг(А)-LS 5×2.5 660В")
    assert key("Кабель ВВГнг(А)-LS 3×2.5") is None
    claims = {"name": "Светильник щитовой", "mpn": "ELM025"}
    assert identity_key(identity_profile(claims | {"brand": "EKF"})) != identity_key(
        identity_profile(claims | {"brand": "Другой бренд"})
    )


def test_provenance_is_not_upgraded_when_catalog_display_value_is_reused():
    """Повторный preview сохраняет происхождение прежнего артикула в профиле."""
    name = "Светильник EKF EXAMPLE"
    answer = legacy_answer("EKF", "EXAMPLE")
    profile = identity_profile({"name": name}, answer)
    row = {
        "product_id": "early",
        "canonical_name": name,
        "created_at": "2026-11-27",
        "updated_at": "2026-11-27",
        "category_id": None,
        "brand_name": "EKF",
        "manufacturer_name": None,
        "mpn": "EXAMPLE",
        "unit_code": None,
        "model": None,
        "stored_answer": answer,
        "stored_profile": profile,
        "fields": {"name": name},
        "answer": answer,
    }
    result = plan_products([row, {**row, "product_id": "later"}], [])
    assert result["insufficient"] == ["early", "later"] and not result["groups"]


@pytest.mark.integration
def test_import_database_error_reports_operation_and_persisted_job(catalog, tmp_path, monkeypatch):
    """HTTP сообщает неудавшуюся операцию и реальный статус задания."""
    path = input_file(tmp_path)
    before = catalog.upload([(path.name, path)])["job"]

    def fail(*args, **kwargs):
        """Имитирует отказ SQL при подготовке входных строк."""
        raise SQLAlchemyError("internal connection diagnostic")

    monkeypatch.setattr(catalog, "start", fail)
    client = TestClient(create_app(catalog))
    response = client.post(
        f"/api/v1/import-jobs/{before['id']}/start", headers={"Idempotency-Key": "failed-start"}
    )
    assert response.status_code == 503
    assert response.json()["job_status"] == "preview_ready"
    assert response.json()["detail"] == (
        "Не удалось подготовить строки и поставить импорт в очередь. Задание готово к запуску."
    )


@pytest.mark.integration
def test_parallel_import_product_reuse_and_category_short_circuit(catalog, tmp_path, monkeypatch):
    """Одинаковый доказанный ключ сериализуется, следующий продавец наследует категорию."""
    jobs = []
    for i in range(2):
        path = input_file(
            tmp_path,
            [
                {
                    "name": f"Светильник EKF ELM025 220В 5Вт исполнение {i}",
                    "mpn": "ELM025",
                    "brand": "EKF",
                    "seller": f"Магазин {i}",
                    "price": "500",
                }
            ],
        )
        job = catalog.upload([(path.name, path)])["job"]["id"]
        catalog.start(job, f"parallel-{i}")
        assert catalog.store.claim_job(job) == job
        jobs.append(job)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(catalog.process, jobs))
    first, second = [catalog.report(job)["items"][0] for job in jobs]
    assert first["product_id"] and first["product_id"] == second["product_id"]
    category = catalog.store.categories()[0]["id"]
    catalog.decide(
        first["id"],
        {"expected_version": first["processing_version"], "category_id": str(category)},
        "test",
        "parallel-set-category",
    )
    original = catalog.inference.answer

    def just_understanding(step, values):
        """Ошибка теста при лишнем запросе на категорию."""
        assert step == 1
        return original(step, values)

    monkeypatch.setattr(catalog.inference, "answer", just_understanding)
    third = run_import(
        catalog,
        input_file(
            tmp_path,
            [
                {
                    "name": "Светильник щитовой EKF 220В 5Вт ELM025",
                    "mpn": "ELM025",
                    "brand": "EKF",
                    "seller": "Третий магазин",
                    "price": "600",
                }
            ],
        ),
    )
    item = catalog.report(third)["items"][0]
    assert item["product_id"] == first["product_id"] and item["category_id"] == str(category)
    with catalog.store.transaction() as unit:
        assert len(unit.all("products")) == 1
        assert len(unit.all("offers", product_id=UUID(first["product_id"]))) == 3


@pytest.mark.integration
def test_merge_keeps_earliest_original_offers_and_progress(catalog, tmp_path):
    """План старого каталога переносит связи и цены, повтор не создаёт новый перенос."""
    names = ["Светильник EKF 220В 5Вт ELM025", "Светильник щитовой ELM025 EKF 5Вт 220В"]
    job = run_import(
        catalog,
        input_file(
            tmp_path,
            [
                {
                    "name": name,
                    "seller": f"Магазин {i}",
                    "price": str(40 + i),
                    "price_date": "2024-08-14",
                    "product_url": f"https://example.org/product/{i}",
                }
                for i, name in enumerate(names)
            ],
        ),
    )
    items = catalog.report(job)["items"]
    first, second = items
    with catalog.store.transaction() as unit:
        for item in items:
            product = unit.get("products", item["product_id"])
            unit.update(
                "products",
                product["id"],
                attributes={
                    **product["attributes"],
                    "understanding": legacy_answer("EKF", "ELM025"),
                },
            )
    merge = ProductMerge(catalog.store)
    plan = merge.preview()
    assert len(plan["groups"]) == 1 and plan["groups"][0]["survivor"] == first["product_id"]
    with pytest.raises(ConflictError, match="новый preview"):
        merge.apply("0" * 64, "negative")
    before = catalog.job(job)
    applied = merge.apply(plan["preview_sha256"], "merge-test")
    assert applied["retired"] == 1 and applied["moved"]["offers"] == 1
    assert merge.apply(plan["preview_sha256"], "merge-test") == applied
    assert not merge.preview()["groups"]
    after = catalog.job(job)
    assert before["processed_count"] == after["processed_count"]
    client = TestClient(create_app(catalog))
    product = client.get(f"/api/v1/products/{second['product_id']}").json()
    assert product["id"] == first["product_id"] and product["canonical_name"] == names[0]
    offers = client.get(f"/api/v1/products/{first['product_id']}/offers").json()["items"]
    assert {offer["original_name"] for offer in offers} == set(names)
    page = client.get("/api/v1/products").json()
    assert page["total"] == 1 and page["items"][0]["seller_count"] == 2
    assert page["items"][0]["historical_price"]["source_reported_date"] == "2024-08-14"
