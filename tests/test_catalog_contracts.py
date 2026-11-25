# tests/test_catalog_contracts.py
"""Контракты форматов, оптимизации дерева и регрессии подтверждённых идентификаторов."""

from uuid import UUID

import pytest

from app.application.taxonomy_review import TaxonomyReview
from app.domain.catalog import (
    CatalogError,
    ConflictError,
    InputRow,
    InvalidModelOutput,
    fingerprint,
    normalize_row,
)
from app.infrastructure.catalog_formats import CatalogFileParser, sample_catalog
from app.settings import Settings
from tests.test_catalog_pipeline import catalog as catalog
from tests.test_catalog_pipeline import input_file, run_import


@pytest.mark.integration
def test_changeset_requires_unchanged_review_and_preserves_ids(catalog):
    """Нет автоматического удаления одиночек; отдельный changeset переносит ссылки и redirect."""
    review = TaxonomyReview(catalog.store)
    before = {row["id"] for row in catalog.store.categories()}
    parent = catalog.create_category(
        {"name": "Родитель одиночки", "reason": "Тест"}, "test", "unary-parent"
    )
    child = catalog.create_category(
        {"name": "Ребёнок одиночки", "parent_id": parent["id"], "reason": "Тест"},
        "test",
        "unary-child",
    )
    with catalog.store.transaction() as unit:
        product = unit.add(
            "products", canonical_name="Тестовый Product одиночки", category_id=UUID(child["id"])
        )
    assert any(row["parent_id"] == parent["id"] for row in review.singletons()["singletons"])
    proposed = review.propose(
        {
            "action": "remove_child",
            "category_id": child["id"],
            "reason": "Подтверждённое тестовое объединение",
        },
        "test",
        "unary-plan",
    )
    assert next(row for row in catalog.store.categories() if str(row["id"]) == child["id"])[
        "is_active"
    ]
    applied = review.apply(proposed["id"], fingerprint(proposed["preview"]), "test", "unary-apply")
    assert applied["status"] == "applied"
    with catalog.store.transaction() as unit:
        assert unit.get("products", product["id"])["category_id"] == UUID(parent["id"])
        assert unit.all("category_redirects", old_category_id=UUID(child["id"]))[0][
            "new_category_id"
        ] == UUID(parent["id"])
    rows = catalog.store.categories()
    assert before <= {row["id"] for row in rows}
    assert not next(row for row in rows if str(row["id"]) == child["id"])["is_active"]
    stale = review.propose(
        {"action": "keep", "category_id": parent["id"], "reason": "Preview устарел"},
        "test",
        "stale-plan",
    )
    with catalog.store.transaction() as unit:
        unit.update("categories", UUID(parent["id"]), name="Родитель изменился")
    with pytest.raises(ConflictError):
        review.apply(stale["id"], fingerprint(stale["preview"]), "test", "stale-apply")


@pytest.mark.integration
def test_mpn_variant_current_and_dimensions_are_not_merged(catalog):
    """Даже confirmed MPN не преодолевает конфликт тока/полюсов/размеров."""
    with catalog.store.transaction() as unit:
        product = unit.add(
            "products", canonical_name="Автомат IEK 16А 2P 220В", status="identified"
        )
        unit.add(
            "product_identifiers",
            product_id=product["id"],
            identifier_type="MPN",
            issuer="iek",
            raw_value="SAME-1",
            normalized_value="same1",
            status="confirmed",
        )
        assert (
            unit.confirmed_product(
                {"brand": "IEK", "mpn": "SAME-1", "name": "Автомат IEK 25А 4P 220В"}
            )
            is None
        )
        assert (
            unit.confirmed_product(
                {"brand": "IEK", "mpn": "SAME-1", "name": "Автомат IEK 16А 2P 220В"}
            )["id"]
            == product["id"]
        )


@pytest.mark.integration
def test_no_seller_sku_cross_offer_merge(catalog, tmp_path):
    """Один SKU у разных продавцов не объединяет Offers или Products."""
    run_import(
        catalog,
        input_file(
            tmp_path,
            [
                {
                    "source_key": "same",
                    "name": "Кабель тестовый один",
                    "seller": "Продавец А",
                    "price": "12",
                },
                {
                    "source_key": "same",
                    "name": "Кабель тестовый два",
                    "seller": "Продавец Б",
                    "price": "13",
                },
            ],
        ),
    )
    with catalog.store.transaction() as unit:
        assert (
            len(unit.all("raw_records"))
            == len(unit.all("offers"))
            == len(unit.all("products"))
            == 2
        )


@pytest.mark.parametrize("format_name", ["json", "xml", "csv", "xlsx"])
def test_download_templates_roundtrip(tmp_path, format_name):
    """Каждый шаблон с сайта принимается тем же производственным parser."""
    payload, content_type = sample_catalog(format_name)
    assert content_type
    path = tmp_path / ("sample." + format_name)
    path.write_bytes(payload)
    rows = CatalogFileParser(Settings()).parse(path, path.name).rows
    normalized = normalize_row(rows[0])
    assert len(rows) == 1 and normalized["fields"]["name"] == "Кабель тестовый"
    assert normalized["fields"]["price"] == "12.50"


@pytest.mark.parametrize("payload", ['{"name":1,"name":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_nonstandard_json_is_rejected(tmp_path, payload):
    """Повтор ключа и нестандартные числовые константы не проходят как корректный JSON."""
    path = tmp_path / "bad.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(CatalogError):
        CatalogFileParser(Settings()).parse(path, path.name)


def test_price_overflow_becomes_issue_before_sql():
    """Переполнение numeric(20,6) отмечается в строке, не обрывает транзакцию всего файла."""
    result = normalize_row(
        InputRow(0, 1, None, {}, {"name": "Кабель тестовый", "price": "999999999999999"})
    )
    assert result["fields"]["price"] is None
    assert any(i["code"] == "invalid_price" for i in result["issues"])


@pytest.mark.integration
def test_invalid_answer2_keeps_product_and_raw_output(catalog, tmp_path, monkeypatch):
    """Ошибка выбора категории сохраняет Product-кандидат, исходный output и задачу проверки."""
    catalog.index.sync()
    original = catalog.inference.answer

    def invalid_selection(step, values):
        """Возвращает корректный answer_1 и заведомо невалидный финальный answer_2."""
        if step == 1:
            return original(step, values)
        raise InvalidModelOutput("invalid_output: синтетическая ошибка", '{"invalid":true}')

    monkeypatch.setattr(catalog.inference, "answer", invalid_selection)
    identifier = run_import(
        catalog, input_file(tmp_path, [{"name": "Кабель тестовый", "price": "12.50"}])
    )
    report = catalog.report(identifier)
    assert report["items"][0]["product_id"]
    assert report["items"][0]["status"] == "needs_review"
    assert report["job"]["error_count"] == 0
    with catalog.store.transaction() as unit:
        decision = unit.all("classification_decisions")[0]
        assert decision["status"] == "needs_review" and decision["category_id"] is None
        assert decision["evidence"]["raw_response_text"] == '{"invalid":true}'
        assert unit.all("review_tasks")[0]["status"] == "open"


@pytest.mark.integration
def test_multifile_source_run_counts_are_separate(catalog, tmp_path):
    """Каждый файл имеет один SourceRun со своим числом строк и общим durable job."""
    first = input_file(tmp_path)
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = input_file(second_dir, [{"name": "Кабель второй тестовый"}])
    preview = catalog.upload([("first.json", first), ("second.json", second)])
    identifier = preview["job"]["id"]
    catalog.start(identifier, "multi-start")
    assert catalog.store.claim_job(identifier)
    catalog.process(identifier)
    with catalog.store.transaction() as unit:
        runs = unit.all("source_runs")
        assert len(runs) == 2 and all(r["status"] == "completed" for r in runs)
        assert sorted(r["counters"]["total_count"] for r in runs) == [1, 3]
        assert sorted(r["counters"]["processed_count"] for r in runs) == [1, 3]


def test_prompt_placeholders_inside_data_are_not_interpreted():
    """Недоверенное имя не раскрывает второй placeholder внутри подставленного JSON."""
    from app.infrastructure.inference import SharedInference

    client = SharedInference(Settings(inference_mode="mock"))
    values = {
        "answer_1_json": {"product_type": "{{category_candidates_json}}"},
        "category_candidates_json": [{"category_id": "literal-category"}],
    }
    prompt = client.render_prompt(2, values)
    assert '"product_type": "{{category_candidates_json}}"' in prompt
    assert prompt.count('"category_id": "literal-category"') == 1


def test_invalid_field_types_do_not_become_product_names_or_paths():
    """Объект вместо имени не становится строковым Product; повреждённый path — только issue."""
    invalid = normalize_row(InputRow(0, 1, None, {}, {"name": {"ru": "Кабель"}}))
    assert invalid["status"] == "invalid" and invalid["fields"]["name"] is None
    derived = normalize_row(
        InputRow(1, 2, None, {}, {"name": "Кабель тестовый", "category_path": 42})
    )
    assert derived["fields"]["category_path"] is None
    assert any(issue["code"] == "invalid_category_path" for issue in derived["issues"])
