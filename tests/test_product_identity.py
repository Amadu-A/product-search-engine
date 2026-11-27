# tests/test_product_identity.py
"""Регрессии доказательств идентичности, параллельного импорта и объединения истории."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.application.product_merge import ProductMerge, plan_products
from app.domain.catalog import ConflictError
from app.domain.product_identity import (
    claim_value,
    decide_identity,
    identity_conflicts,
    identity_key,
    identity_profile,
    identity_retrieval_keys,
    with_verified_reference,
)
from app.infrastructure.catalog_store import CatalogSession
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


def test_incomplete_code_requires_independent_evidence():
    """Один артикул, код в URL и подсказка модели дают кандидата без слияния."""
    known = identity_profile(
        {"name": "Лоток Экватор 200×50×3000", "brand": "Экватор", "mpn": "01450"}
    )
    partial = identity_profile(
        {"name": "Лоток 200х50х3000 (01450)", "origin_url": "https://seller.example/01450"}
    )
    document = {
        "claims": {"brand": "Экватор", "mpn": "01450"},
        "document": "https://maker.example/item/01450",
        "type_terms": ["лоток"],
        "features": {"dimensions": ["200x50x3000"]},
    }
    assert set(identity_retrieval_keys(known)) & set(identity_retrieval_keys(partial))
    assert decide_identity(partial, [("known", known)])["decision"] == "uncertain"
    confirmed = with_verified_reference(partial, document)
    assert decide_identity(confirmed, [("known", known)])["product_id"] == "known"
    for wrong in ("Лоток 50х200х3000 (01450)", "Лоток 200х50х3000 (1450)"):
        original = identity_profile({"name": wrong})
        assert with_verified_reference(original, document) == original
    assert (
        decide_identity(confirmed, [("known", known)], overflow=True)["reason"] == "candidate_limit"
    )


@pytest.mark.parametrize(
    "baseline, variant, field",
    [
        ("IP66", "IP67", "ip"),
        ("магнитный", "винтовой", "mounting"),
        ("ширина 5 см", "ширина 60 мм", "width"),
        ("серый", "белый", "color"),
        ("C16 2P", "B16 4P", "trip_rating"),
    ],
)
def test_hard_variant_conflicts_override_equal_mpn(baseline, variant, field):
    """Известные признаки исполнения блокируют даже подтверждённый ключ."""
    known = {"brand": "Экватор", "mpn": "PSE-001"}
    first = identity_profile({**known, "name": "Выключатель Экватор " + baseline})
    second = identity_profile({**known, "name": "Выключатель Экватор " + variant})
    decision = decide_identity(second, [("first", first)])
    assert decision["decision"] == "different" and field in decision["candidates"][0]["conflicts"]


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
def test_merge_keeps_earliest_original_offers_and_progress(catalog, tmp_path, monkeypatch):
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
        alias = unit.add("products", canonical_name="Старый алиас", status="retired")
        ordinary = unit.add("products", canonical_name="Ручное удаление", status="retired")
        unit.connection.execute(
            unit.table("product_merge_redirects")
            .insert()
            .values(
                old_product_id=alias["id"],
                product_id=UUID(second["product_id"]),
                evidence={"source": "test"},
            )
        )
        prices = unit.all("price_observations")
        point = prices[-1]
        verified = unit.add(
            "price_history",
            offer_id=point["offer_id"],
            price_observation_id=point["id"],
            amount=point["amount"],
            currency="RUB",
            sale_unit="шт",
            price_type="retail",
            valid_from=datetime(2024, 8, 14, tzinfo=UTC),
            verified_by="fixture",
        )
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
    original = CatalogSession.merge_products

    def interruption(unit, group):
        """Имитирует сбой после физического удаления в транзакции."""
        original(unit, group)
        raise ConflictError("Проверка rollback")

    with monkeypatch.context() as context:
        context.setattr(CatalogSession, "merge_products", interruption)
        with pytest.raises(ConflictError, match="rollback"):
            merge.apply(plan["preview_sha256"], "rollback-test")
    assert merge.preview()["preview_sha256"] == plan["preview_sha256"]
    before = catalog.job(job)
    applied = merge.apply(plan["preview_sha256"], "merge-test")
    assert applied["retired"] == 1 and applied["moved"]["offers"] == 1
    assert applied["deleted"] == 2 and applied["legacy_deleted"] == 1
    assert merge.apply(plan["preview_sha256"], "merge-test") == applied
    assert not merge.preview()["groups"]
    with catalog.store.readonly() as unit:
        assert not unit.all("products", id=UUID(second["product_id"]))
        assert not unit.all("products", id=alias["id"])
        assert unit.get("products", ordinary["id"])["status"] == "retired"
        assert unit.get("price_history", verified["id"]) == verified
        assert unit.resolve_product_id(alias["id"]) == UUID(first["product_id"])
        assert unit.resolve_product_id(UUID(second["product_id"])) == UUID(first["product_id"])
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
