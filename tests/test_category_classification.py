# tests/test_category_classification.py
"""Регрессии автоматического назначения, допуска широких категорий и аудита VLM."""

import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.application.category_selection import (
    CategorySelection,
    candidate_view,
    suggested_category_ids,
)
from app.domain.catalog import (
    DependencyUnavailable,
    InvalidModelOutput,
    approved_category_exclusion,
)
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
        if step == 2 and "Лоток" in values["answer_1_json"]["source_product_name"]:
            return {
                "status": "no_suitable_category",
                "category_id": None,
                "selection_type": "none",
                "reason": "Назначение изделия не подтверждает кабельную ветку",
                "supporting_evidence_ids": [],
                "compared_candidate_ids": [],
            }
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
                {"name": "Лоток почкообразный"},
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
    "name,conflict",
    [
        ("Лоток почкообразный ЛМП-160", True),
        ("Лоток хирургический", True),
        ("Лоток кабельный 150×50", False),
    ],
)
def test_approved_category_exclusion(name, conflict):
    """Фразы берутся из данных узла и предков, а не из каталога исключений в коде."""
    category = {
        "exclusions": "Лоток хирургический",
        "ancestor_exclusions": ["Лоток почкообразный\nВетеринарный лоток"],
    }
    assert bool(approved_category_exclusion(name, category)) is conflict


@pytest.mark.integration
def test_bath_accessories_are_not_grounded_by_the_shape_of_a_tray(catalog):
    """Вид изделия сам по себе не подтверждает узкое назначение выбранной ветки."""
    candidate = {
        "category_id": "candidate-from-tree",
        "path_names": ["Сантехника", "Аксессуары для ванной комнаты"],
        "review_status": "provisional",
    }
    result = catalog.classifier.verify_hierarchy(
        CategorySelection(category=candidate, strategy="hierarchical"),
        {"name": "Лоток почкообразный"},
        {"product_type": "Лоток почкообразный", "purpose": None, "applications": []},
    )
    assert result.category is None
    assert result.reason == "Исходные сведения не подтверждают назначение ветки"


def test_review_suggestions_use_only_compared_and_not_rejected_categories():
    """Предложения берутся из ответов моделей и ограничены входами соответствующего шага."""
    decision = {
        "status": "needs_review",
        "category_id": None,
        "candidates": [{"category_id": "hierarchy"}],
        "evidence": {
            "hybrid": {
                "candidates": [{"category_id": "hybrid"}, {"category_id": "uncompared"}],
                "answer_2": {
                    "status": "needs_review",
                    "compared_candidate_ids": ["hybrid", "outside"],
                },
            },
            "hierarchical_steps": [
                {
                    "allowed_ids": ["hierarchy"],
                    "answer_3": {"action": "select_child", "category_id": "hierarchy"},
                },
                {
                    "allowed_ids": [],
                    "answer_3": {"action": "select_current", "category_id": "hierarchy"},
                },
            ],
        },
    }
    assert suggested_category_ids(decision) == ["hierarchy", "hybrid"]
    decision["evidence"].update(
        semantic_check={"status": "no_suitable_category"},
        semantic_check_candidates=[{"category_id": "hierarchy"}],
    )
    assert suggested_category_ids(decision) == ["hybrid"]


@pytest.mark.integration
def test_scope_check_can_accept_valid_parent_of_overly_narrow_choice(catalog, monkeypatch):
    """Ответ 2 сравнивает подтверждённую широкую категорию с ошибочным узким выбором."""
    root = catalog.create_category({"name": "Электрика", "reason": "Фикстура"}, "test", "electric")
    wires = catalog.create_category(
        {"name": "Кабель и провод", "parent_id": root["id"], "reason": "Фикстура"},
        "test",
        "wires",
    )
    power = catalog.create_category(
        {"name": "Силовой кабель", "parent_id": wires["id"], "reason": "Фикстура"},
        "test",
        "power",
    )
    rows = catalog.store.categories()
    candidate = candidate_view(next(row for row in rows if str(row["id"]) == power["id"]))

    def compare(step, values):
        """Не подтверждает подтип, для которого в наименовании недостаточно сведений."""
        assert step == 2
        options = values["category_candidates_json"]
        allowed = [entry["category_id"] for entry in options]
        if allowed == [power["id"]]:
            return {
                "status": "no_suitable_category",
                "category_id": None,
                "selection_type": "none",
                "reason": "Силовой тип не подтверждён",
                "supporting_evidence_ids": [],
                "compared_candidate_ids": allowed,
            }
        assert allowed == [wires["id"]]
        return {
            "status": "selected",
            "category_id": wires["id"],
            "selection_type": "broader",
            "reason": "Это кабель, подтип не подтверждён",
            "supporting_evidence_ids": ["raw_name"],
            "compared_candidate_ids": allowed,
        }

    monkeypatch.setattr(catalog.inference, "mock_answer", compare)
    with catalog.inference.budget() as usage:
        result = catalog.classifier.verify_hierarchy(
            CategorySelection(category=candidate, selection_type="exact", strategy="hierarchical"),
            {"name": "Кабель связи"},
            {"product_type": "Кабель", "evidence_ids": ["raw_name"], "attributes": []},
            rows,
        )
    assert result.category["category_id"] == wires["id"]
    assert result.selection_type == "broader"
    assert usage["vlm_requests"] == 2
    assert result.evidence["semantic_check_declined_ids"] == [power["id"]]


@pytest.mark.integration
@pytest.mark.parametrize("excluded", [False, True])
def test_combined_checks_hybrid_then_descends_or_falls_back(
    catalog, tmp_path, monkeypatch, excluded
):
    """Широкий узел уточняется, утверждённое исключение запускает обход от корня."""
    name = "Кабель силовой ВВГнг"
    root = catalog.create_category(
        {
            "name": "Кабель, провод",
            "reason": "Фикстура",
            "exclusions": name if excluded else None,
        },
        "test",
        "root",
    )
    leaf = catalog.create_category(
        {"name": "Силовой кабель", "parent_id": root["id"], "reason": "Фикстура"},
        "test",
        "leaf",
    )
    ancestor = next(r for r in catalog.store.categories() if str(r["id"]) == root["id"])
    monkeypatch.setattr(catalog.index, "candidates", lambda _: ([candidate_view(ancestor)], []))
    original = catalog.inference.mock_answer
    calls = []

    def answer(step, values):
        """Проверяет фактический candidate-set, переданный модели на следующем шаге."""
        if step != 3:
            return original(step, values)
        current = values["current_category_json"]
        options = {r["category_id"] for r in values["child_categories_json"]}
        calls.append((current["category_id"] if current else None, options))
        if excluded:
            assert current is None and root["id"] not in options
            return {"action": "no_match", "category_id": None, "reason": "Ветка исключена"}
        if current["category_id"] == root["id"]:
            assert options == {leaf["id"]}
            return {"action": "select_child", "category_id": leaf["id"], "reason": "Тип кабеля"}
        return {"action": "select_current", "category_id": leaf["id"], "reason": "Тип кабеля"}

    monkeypatch.setattr(catalog.inference, "mock_answer", answer)
    job = run_import(catalog, input_file(tmp_path, [{"name": name}]))
    item = catalog.report(job)["items"][0]
    assert calls
    assert item["category_id"] == (None if excluded else leaf["id"])
    with catalog.store.transaction() as unit:
        evidence = unit.all("classification_decisions")[0]["evidence"]
        assert evidence["vlm_requests"] <= catalog.settings.classification_vlm_budget
        if excluded:
            assert (
                evidence["hybrid"]["purpose_conflict"]["rejected_category"]["category_id"]
                == root["id"]
            )


@pytest.mark.integration
def test_answer1_raw_name_creates_standalone_product(catalog, tmp_path, monkeypatch):
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
    assert selected["assignment_origin"] == "auto" and selected["product_id"]
    assert bare["status"] == "needs_review" and bare["category_id"] is None
    assert report["job"]["technical_error_count"] == 1
    assert report["database_counts"]["products"] == 1
    assert (
        report["job"]["products_created_count"] == report["job"]["categorized_products_count"] == 1
    )
    with catalog.store.transaction() as unit:
        decision = next(row for row in unit.all("classification_decisions") if row["category_id"])
        assert decision["evidence"]["understanding_origin"] == "raw_name"
        assert decision["evidence"]["answer_1_issues"][0]["code"] == "schema"
        product = unit.get("products", selected["product_id"])
        assert (
            product["status"] == "provisional"
            and str(product["category_id"]) == selected["category_id"]
        )
    repeated = run_import(catalog, input_file(tmp_path, [{"name": "Цемент строительный"}]))
    repeated_report = catalog.report(repeated)
    assert repeated_report["items"][0]["product_id"] == selected["product_id"]
    assert repeated_report["job"]["products_created_count"] == 0
    assert repeated_report["job"]["categorized_products_count"] == 1


@pytest.mark.integration
def test_reclassify_fills_legacy_assignments_without_file_or_inference(
    catalog, tmp_path, monkeypatch
):
    """Два задания одной строки получают общий Product, ручная категория сохраняется."""
    path = input_file(tmp_path, [{"name": "Цемент строительный"}, {"name": "Кабель тестовый"}])
    jobs = [catalog.upload([(path.name, path)])["job"]["id"] for _ in range(2)]
    category = catalog.store.categories()[0]
    with catalog.store.transaction() as unit:
        assert not unit.all("products")
    for job in jobs:
        catalog.start(job, "legacy-" + job)
        with catalog.store.transaction() as unit:
            for row in unit.all("ingest_job_items", job_id=UUID(job)):
                unit.update(
                    "ingest_job_items", row["id"], status="assigned", category_id=category["id"]
                )
                if job == jobs[0]:
                    unit.add(
                        "classification_decisions",
                        normalized_record_id=row["normalized_record_id"],
                        category_id=category["id"],
                        status="approved",
                        selection_type="exact",
                        method="manual" if row["row_number"] == 1 else "retrieval_vlm",
                        decided_by="reviewer" if row["row_number"] == 1 else "system",
                    )
            for file in unit.all("ingest_job_files", job_id=UUID(job)):
                Path(file["storage_key"]).unlink()
            unit.refresh_counts(job)
            unit.update("ingest_jobs", job, status="completed")

    def unavailable(*args):
        """Гарантирует восстановление только по сохранённому решению."""
        pytest.fail("Для восстановления не нужны запросы VLM")

    monkeypatch.setattr(catalog.inference, "answer", unavailable)
    assert catalog.job(jobs[0])["assigned_without_product_count"] == 2
    assert catalog.job(jobs[0])["status"] == "completed_with_issues"
    assert catalog.reclassify(jobs[0])["eligible"] == 2
    assert catalog.reclassify(jobs[0], apply=True)["queued"]
    assert catalog.store.claim_job(jobs[0])
    catalog.process(jobs[0])
    first, repeated = [catalog.report(job) for job in jobs]
    assert first["job"]["manual_assigned_count"] == 1
    assert first["job"]["products_created_count"] == 2
    assert repeated["job"]["products_created_count"] == 0
    assert repeated["job"]["categorized_products_count"] == 2
    assert first["database_counts"]["products"] == first["database_counts"]["raw_records"] == 2
    assert [row["product_id"] for row in first["items"]] == [
        row["product_id"] for row in repeated["items"]
    ]
    assert catalog.reclassify(jobs[1])["eligible"] == 0
    with catalog.store.transaction() as unit:
        manual = unit.all("classification_decisions", method="manual")[0]
        assert manual["decided_by"] == "reviewer" and manual["category_id"] == category["id"]


@pytest.mark.integration
def test_manual_assignment_and_reject_keep_a_standalone_product(catalog, tmp_path):
    """Ручная категоризация материализует конкретную позицию, отказ от связи не сливает товары."""
    path = input_file(tmp_path, [{"name": "Выключатель концевой"}])
    job = catalog.upload([(path.name, path)])["job"]["id"]
    catalog.start(job, "fresh-manual")
    row = catalog.report(job)["items"][0]
    category = catalog.store.categories()[0]
    assigned = catalog.decide(
        row["id"],
        {"expected_version": 1, "category_id": str(category["id"]), "reason": "Проверено"},
        "operator",
        "assign",
    )
    updated = catalog.decide(
        row["id"],
        {"expected_version": 2, "action": "reject_product", "reason": "Отдельный товар"},
        "operator",
        "reject",
    )
    assert assigned["product_id"] and updated["product_id"]
    assert updated["product_id"] != assigned["product_id"]
    assert updated["status"] == "assigned" and updated["category_id"] == assigned["category_id"]


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
