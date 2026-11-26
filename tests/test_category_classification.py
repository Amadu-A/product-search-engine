# tests/test_category_classification.py
"""Регрессии автоматического назначения, допуска широких категорий и аудита VLM."""

import json

import httpx
import pytest

from app.domain.catalog import DependencyUnavailable, InvalidModelOutput, category_purpose_conflict
from app.infrastructure.inference import SharedInference
from app.settings import Settings
from tests.test_catalog_pipeline import catalog as catalog
from tests.test_catalog_pipeline import input_file, run_import


def test_one_repair_preserves_invalid_json_and_finish_reason():
    """Обрыв диагностируется отдельно, второй запрос получает конкретное исправление."""
    client = SharedInference(Settings(inference_mode="live", shared_vlm_model_revision="test"))
    payloads = []

    def completion(request):
        """Имитирует два финальных ответа OpenAI-совместимого API."""
        payloads.append(json.loads(request.content))
        content, finish = (
            ('{"action":', "length")
            if len(payloads) == 1
            else ('{"action":"no_match","category_id":null,"reason":"Нет совпадения"}', "stop")
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}]}
        )

    client.client = httpx.Client(transport=httpx.MockTransport(completion))
    with client.budget() as usage:
        result = client.answer(
            3,
            {
                "normalized_record_json": {"name": "Медицинский лоток"},
                "answer_1_json": {},
                "current_category_json": None,
                "child_categories_json": [],
            },
        )
    assert usage["vlm_requests"] == 2
    assert result["attempts"] == [
        {
            "code": "truncated",
            "step": 3,
            "reason": "Генерация оборвалась по лимиту",
            "raw_response_text": '{"action":',
            "finish_reason": "length",
        }
    ]
    assert "Генерация оборвалась по лимиту" in payloads[1]["messages"][0]["content"]


@pytest.mark.parametrize(
    "raw,code",
    [
        ("{", "json"),
        ('{"action":"no_match"}', "schema"),
        ('{"action":"select_child","category_id":"missing","reason":"test"}', "uuid"),
        (
            '{"action":"select_child","category_id":"bb683614-e371-4c1a-863e-7d54daf75a40","reason":"test"}',
            "uuid",
        ),
    ],
)
def test_answer3_output_diagnostics(raw, code):
    """Проверка JSON, структуры и принадлежности кандидату выполняется независимо."""
    client = SharedInference(Settings(inference_mode="mock"))
    with pytest.raises(InvalidModelOutput) as failure:
        client.validate_answer(
            3, {"current_category_json": None, "child_categories_json": []}, raw, "stop"
        )
    assert failure.value.code == code


@pytest.mark.integration
def test_selected_broader_assigns_without_optional_fields(catalog, tmp_path):
    """Реальные SQL-решение и Product получают категорию при broader без цены или MPN."""
    catalog.index.sync()
    job = run_import(catalog, input_file(tmp_path, [{"name": "Цемент строительный"}]))
    report = catalog.report(job)
    item = report["items"][0]
    assert report["job"]["auto_assigned_count"] == 1
    assert report["job"]["manual_assigned_count"] == 0
    assert item["status"] == "assigned" and item["assignment_origin"] == "auto"
    with catalog.store.transaction() as unit:
        product = unit.get("products", item["product_id"])
        decision = unit.all("classification_decisions")[0]
        assert str(product["category_id"]) == item["category_id"]
        assert decision["status"] == "approved" and decision["decided_by"] == "system"
        assert decision["selection_type"] == "broader"
        assert decision["evidence"]["mode"] == "mock"


@pytest.mark.integration
@pytest.mark.parametrize("scenario", ["empty", "outage"])
def test_hierarchy_stops_on_broader_and_rejects_wrong_use(catalog, tmp_path, monkeypatch, scenario):
    """При пустой выдаче или недоступном Qdrant используются непосредственные дети SQL."""
    root = catalog.create_category({"name": "Электрика", "reason": "Фикстура"}, "test", "root")
    broad = catalog.create_category(
        {"name": "Выключатели", "parent_id": root["id"], "reason": "Фикстура"}, "test", "broad"
    )
    catalog.create_category(
        {"name": "Сенсорные выключатели", "parent_id": broad["id"], "reason": "Фикстура"},
        "test",
        "leaf",
    )
    tray = catalog.create_category(
        {"name": "Лотки кабельные", "parent_id": root["id"], "reason": "Фикстура"}, "test", "tray"
    )
    catalog.index.sync()

    def index_output(*args):
        """Эмулирует пустой индекс или недоступность его HTTP-операций."""
        if scenario == "outage":
            raise DependencyUnavailable("Изолированный Qdrant outage")
        return [], []

    target = "request" if scenario == "outage" else "candidates"
    monkeypatch.setattr(catalog.index, target, index_output)
    original = catalog.inference.mock_answer

    def answer(step, values):
        """Модель выбирает широкую ветку и ошибается на омониме «лоток»."""
        if step != 3:
            return original(step, values)
        current = values["current_category_json"]
        children = {r["category_id"] for r in values["child_categories_json"]}
        tray_name = "лоток" in values["normalized_record_json"]["name"].casefold()
        if current is None:
            assert root["id"] in children
            return {"action": "select_child", "category_id": root["id"], "reason": "Электрика"}
        if current["category_id"] == root["id"]:
            assert broad["id"] in children
            return {
                "action": "select_child",
                "category_id": tray["id"] if tray_name else broad["id"],
                "reason": "Предполагаемая категория",
            }
        if tray_name:
            return {"action": "select_current", "category_id": tray["id"], "reason": "Лотки"}
        assert current["category_id"] == broad["id"] and children
        return {"action": "select_current", "category_id": broad["id"], "reason": "Концевой тип"}

    monkeypatch.setattr(catalog.inference, "mock_answer", answer)
    job = run_import(
        catalog,
        input_file(
            tmp_path,
            [
                {"name": "Выключатель концевой"},
                {"name": "Лоток медицинский"},
            ],
        ),
    )
    report = catalog.report(job)
    assert report["items"][0]["category_id"] == broad["id"]
    assert report["items"][1]["status"] == "needs_review"
    assert report["items"][1]["category_id"] is None
    with catalog.store.transaction() as unit:
        events = unit.events(job, 0)
        assert len([event for event in events if event["type"] == "classification.fallback"]) == 2
        decisions = unit.all("classification_decisions")
        assert any("purpose_conflict" in decision["evidence"] for decision in decisions)


@pytest.mark.integration
def test_reclassify_old_proposed_preserves_row_identity_and_manual(catalog, tmp_path, monkeypatch):
    """Повтор использует SQL, старый answer_1 и прежний Product, сохраняя ручные решения."""
    original = catalog.index.candidates
    monkeypatch.setattr(catalog.index, "candidates", lambda _: ([], []))
    job = run_import(
        catalog,
        input_file(
            tmp_path,
            [
                {"name": "Цемент строительный"},
                {"name": "Штукатурка гипсовая"},
            ],
        ),
    )
    first, second = catalog.report(job)["items"]
    category = catalog.store.categories()[0]
    with catalog.store.transaction() as unit:
        unit.add(
            "classification_decisions",
            normalized_record_id=first["normalized_record_id"],
            product_id=first["product_id"],
            category_id=category["id"],
            status="proposed",
            method="retrieval_vlm",
            selection_type="broader",
        )
    catalog.decide(
        second["id"],
        {
            "category_id": str(category["id"]),
            "expected_version": 1,
            "reason": "Ручная проверка",
        },
        "test",
        "manual",
    )
    catalog.index.sync()
    monkeypatch.setattr(catalog.index, "candidates", original)
    first_answer = catalog.inference.answer

    def no_answer1(step, values):
        """Проверяет, что сохранённый answer_1 повторно не генерируется."""
        assert step != 1
        return first_answer(step, values)

    monkeypatch.setattr(catalog.inference, "answer", no_answer1)
    assert catalog.reclassify(job)["eligible"] == 1
    assert catalog.reclassify(job, apply=True)["queued"]
    assert catalog.store.claim_job(job) == job
    catalog.process(job)
    result = catalog.report(job)
    assert result["job"]["auto_assigned_count"] == result["job"]["manual_assigned_count"] == 1
    assert result["database_counts"]["products"] == result["database_counts"]["raw_records"] == 2
    assert result["items"][0]["product_id"] == first["product_id"]
    assert result["items"][1]["category_id"] == str(category["id"])
    assert catalog.reclassify(job, apply=True)["eligible"] == 0


def test_budget_stops_before_unbounded_hierarchy():
    """Бюджет исчерпывается до нового вызова VLM."""
    client = SharedInference(Settings(inference_mode="mock", classification_vlm_budget=3))
    with client.budget() as usage:
        for _ in range(3):
            client.consume_request()
        with pytest.raises(DependencyUnavailable, match="бюджет"):
            client.consume_request()
    assert usage["vlm_requests"] == 3


@pytest.mark.parametrize(
    "name,path,conflict",
    [
        ("Лоток хирургический", ["Электрика", "Кабельные лотки"], True),
        ("Почкообразный лоток из нержавеющей стали", ["Инструмент", "Малярный"], True),
        ("Лоток для стерилизации", ["Медицинские изделия", "Контейнеры"], False),
        ("Лоток кабельный 150×50", ["Электрика", "Кабельные лотки"], False),
        ("Провод для медицинского оборудования", ["Электрика", "Провод"], False),
    ],
)
def test_domain_conflict_without_label_or_supplier(name, path, conflict):
    """Проверяет термин назначения по исходному имени и полному пути."""
    assert bool(category_purpose_conflict(name, path)) is conflict


@pytest.mark.integration
def test_answer1_raw_name_can_classify_without_creating_product(catalog, tmp_path, monkeypatch):
    """После технического отказа используются явные факты исходного имени."""
    catalog.index.sync()
    answer = catalog.inference.answer

    def broken_understanding(step, values):
        """Проваливает распознавание, сохраняя рабочий выбор категории."""
        if step == 1:
            raise InvalidModelOutput("Ошибка схемы", "{", code="schema", step=1)
        return answer(step, values)

    monkeypatch.setattr(catalog.inference, "answer", broken_understanding)
    job = run_import(
        catalog,
        input_file(
            tmp_path,
            [
                {"name": "Цемент строительный"},
                {"name": "ABC-123"},
            ],
        ),
    )
    report = catalog.report(job)
    selected, bare = report["items"]
    assert selected["assignment_origin"] == "auto" and selected["product_id"] is None
    assert bare["status"] == "needs_review" and bare["category_id"] is None
    assert report["job"]["technical_error_count"] == 1
    assert report["database_counts"]["products"] == 0
    with catalog.store.transaction() as unit:
        decision = next(row for row in unit.all("classification_decisions") if row["category_id"])
        assert decision["evidence"]["understanding_origin"] == "raw_name"
        assert decision["evidence"]["answer_1_issues"][0]["code"] == "schema"


@pytest.mark.integration
def test_inactive_manual_category_on_existing_product_is_preserved(catalog, tmp_path):
    """Автоматический импорт не заменяет недоступное ручное назначение с другой строки."""
    catalog.index.sync()
    name = "Цемент строительный"
    first = run_import(
        catalog,
        input_file(
            tmp_path,
            [
                {"name": name, "brand": "Test", "mpn": "T-1"},
            ],
        ),
    )
    row = catalog.report(first)["items"][0]
    category_id = row["category_id"]
    for action in ("assign_category", "confirm_product"):
        current = catalog.report(first)["items"][0]
        catalog.decide(
            row["id"],
            {
                "action": action,
                "category_id": category_id,
                "expected_version": current["processing_version"],
                "reason": "Ручная проверка",
            },
            "test",
            action,
        )
    with catalog.store.transaction() as unit:
        unit.update("categories", category_id, is_active=False)
    second = run_import(
        catalog,
        input_file(
            tmp_path,
            [
                {"name": name, "brand": "Test", "mpn": "T-1", "price": "14.50"},
            ],
        ),
    )
    later = catalog.report(second)["items"][0]
    assert later["status"] == "needs_review" and later["product_id"] == row["product_id"]
    with catalog.store.transaction() as unit:
        assert str(unit.get("products", row["product_id"])["category_id"]) == category_id
