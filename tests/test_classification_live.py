# tests/test_classification_live.py
"""Сравнивает три политики на AITIM-200 с живыми shared VLM и embedding."""

import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import pytest

from app.catalog_bootstrap import build_catalog
from app.domain.catalog import (
    DependencyUnavailable,
    InvalidModelOutput,
    is_bare_identifier,
    raw_type_understanding,
)
from app.settings import Settings
from tests.catalog_fixture import restore_categories

pytestmark = [pytest.mark.integration, pytest.mark.live]


def diagnostics(value):
    """Извлекает все учтённые технические попытки, включая исправленные."""
    if isinstance(value, list):
        for item in value:
            yield from diagnostics(item)
    elif isinstance(value, dict):
        if {"step", "code", "raw_response_text"} <= value.keys():
            yield value
        else:
            for item in value.values():
                yield from diagnostics(item)


def silent_event(kind, message, data):
    """В сравнении наблюдаемые шаги хранятся в audit, без SSE-записей трёх прогонов."""


def compare_row(service, item):
    """Подаёт общий answer_1 всем стратегиям, учитывая его цену в каждой."""
    with service.store.transaction() as unit:
        norm = unit.get("normalized_records", item["normalized_record_id"])
    fields = norm["attributes"]["fields"]
    start = perf_counter()
    with service.inference.budget() as common:
        try:
            answer = service.understand(item, norm, common)
        except InvalidModelOutput:
            answer = raw_type_understanding(fields)
            if answer:
                common["understanding_origin"] = "raw_name"
        except DependencyUnavailable as error:
            answer = None
            common["dependency_error"] = str(error)
    common_seconds = perf_counter() - start
    decisions = {}
    eligible = (
        answer
        and answer["status"] == "recognized"
        and answer["product_type"]
        and answer["record_kind"] == "product_candidate"
        and not is_bare_identifier(fields)
    )
    strategies = ("hybrid", "hierarchical", "combined")
    shift = item["item_position"] % len(strategies)
    for strategy in (*strategies[shift:], *strategies[:shift]):
        with service.inference.budget() as usage:
            usage["vlm_requests"] = common["vlm_requests"]
            started = perf_counter()
            result = (
                service.classifier.select(fields, answer, silent_event, strategy)
                if eligible
                else None
            )
            if result and result.category:
                with service.store.transaction() as unit:
                    row = unit.assignable_path(result.category["category_id"])
                    assert row and row["path_ids"] == result.category["path_ids"]
            decisions[strategy] = {
                "vlm_requests": usage["vlm_requests"],
                "seconds": common_seconds + perf_counter() - started,
                "category": result.category if result else None,
                "reason": result.reason
                if result
                else common.get("dependency_error", "Тип изделия не подтверждён"),
                "evidence": result.evidence if result else {},
            }
    return {
        "row": item["row_number"],
        "answer_1": answer,
        "common": common,
        "strategies": decisions,
    }


def summarize(rows, gold):
    """Считает измеренные показатели и применяет только предоставленную Gold-разметку."""
    output = {}
    for strategy in ("hybrid", "hierarchical", "combined"):
        results = [row["strategies"][strategy] for row in rows]
        errors = Counter(
            (str(issue["step"]), issue["code"])
            for row, result in zip(rows, results, strict=True)
            for issue in diagnostics([row["common"], result["evidence"]])
        )
        classified = sum(bool(r["category"]) for r in results)
        checked = [
            (gold[str(row["row"])], r)
            for row, r in zip(rows, results, strict=True)
            if str(row["row"]) in gold
        ]
        checked_assigned = sum(bool(r["category"]) for _, r in checked)
        relevant = sum(bool(ids) for ids, _ in checked)
        correct = sum(
            bool(r["category"] and r["category"]["category_id"] in ids) for ids, r in checked
        )
        requests = sum(r["vlm_requests"] for r in results)
        failure_records = sum(
            bool(list(diagnostics([row["common"], r["evidence"]])))
            for row, r in zip(rows, results, strict=True)
        )
        output[strategy] = {
            "assigned": classified,
            "assignment_rate": classified / len(rows),
            "vlm_requests": requests,
            "sum_row_seconds": round(sum(r["seconds"] for r in results), 2),
            "invalid_attempts": {"/".join(key): count for key, count in errors.items()},
            "records_with_invalid_output": failure_records,
            "invalid_output_row_rate": failure_records / len(rows),
            "gold_rows": len(checked),
            "gold_assigned": checked_assigned,
            "gold_classifiable": relevant,
            "precision": correct / checked_assigned if checked_assigned else None,
            "recall": correct / relevant if relevant else None,
        }
    return output


def test_aitim200_classification_comparison(db_engine, tmp_path):
    """Обрабатывает настоящие 200 строк и сохраняет наблюдения после каждой строки."""
    if not Settings().live_smoke_enabled:
        pytest.skip("Сравнение требует отдельного --classification-live")
    path = Path("examples/aitim_200_raw.json")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert sha == "a160f659c44bab5f36623d629a9740882ccb2ec5b7f8683bb06ae5b4fded8f28"
    assert restore_categories(db_engine)["categories"] == 2540
    settings = Settings(
        inference_mode="live",
        job_transport="postgres",
        upload_dir=tmp_path / "uploads",
        qdrant_collection_alias="product_taxonomy_categories_comparison_" + uuid4().hex,
    )
    service = build_catalog(settings, db_engine)
    index = service.index.sync()
    assert index["points"] == 2540
    identifier = service.upload([(path.name, path)])["job"]["id"]
    service.start(identifier, "comparison")
    with service.store.transaction() as unit:
        items = sorted(
            unit.all("ingest_job_items", job_id=UUID(identifier)), key=lambda row: row["row_number"]
        )
    assert len(items) == 200
    gold_path = Path("examples/aitim_200_gold.json")
    gold = json.loads(gold_path.read_bytes()) if gold_path.is_file() else {}
    rows = []
    reports = Path(".tmp/reports")
    reports.mkdir(parents=True, exist_ok=True)
    begun = perf_counter()
    with (
        ThreadPoolExecutor(max_workers=settings.inference_concurrency) as pool,
        (reports / "classification-aitim200.jsonl").open("w", encoding="utf-8") as stream,
    ):
        for row in pool.map(lambda item: compare_row(service, item), items):
            rows.append(row)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            if len(rows) % 10 == 0:
                print(f"AITIM-200: {len(rows)}/200", flush=True)
    summary = {
        "mode": "LIVE",
        "source_sha256": sha,
        "rows": len(rows),
        "points": index["points"],
        "embedding_dimensions": index["embedding_dimensions"],
        "vlm_revision": service.inference.model_revision,
        "elapsed_seconds": perf_counter() - begun,
        "classification": summarize(rows, gold),
    }
    (reports / "classification-aitim200.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
