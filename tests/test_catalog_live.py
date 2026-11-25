# tests/test_catalog_live.py
"""Отдельный короткий LIVE-контракт финального клиента shared VLM/embedding."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.catalog_bootstrap import build_catalog
from app.domain.catalog import json_value
from app.settings import Settings
from tests.test_catalog_pipeline import input_file, run_import
from tests.test_postgres import importer

pytestmark = [pytest.mark.integration, pytest.mark.live]


def test_final_shared_contract(db_engine, snapshot, tmp_path):
    """Проверяет реальные два ответа, embedding, Qdrant и SQL на собственном малом наборе."""
    if not Settings().live_smoke_enabled:
        pytest.skip("Отдельный LIVE smoke требует LIVE_SMOKE_ENABLED=true")
    importer(db_engine).execute(snapshot, apply=True)
    settings = Settings(
        inference_mode="live",
        job_transport="postgres",
        upload_dir=tmp_path / "uploads",
        qdrant_collection_alias="product_taxonomy_categories_live_" + uuid4().hex,
    )
    service = build_catalog(settings, db_engine)
    try:
        service.inference.check_availability()
        indexed = service.index.sync()
        assert indexed["points"] == 8
        assert indexed["embedding_dimensions"] == settings.embedding_dimensions
        assert not indexed["embedding_model_revision"].startswith("mock")
        path = input_file(
            tmp_path,
            [
                {
                    "name": "Цемент строительный портландцемент М500",
                    "unit": "кг",
                    "price": "12.50",
                    "currency": "RUB",
                }
            ],
        )
        identifier = run_import(service, path)
        with service.store.transaction() as unit:
            understanding = unit.all("product_understandings")[0]
            decisions = unit.all("classification_decisions")
            assert understanding["status"] == "recognized"
            assert understanding["model_revision"] not in {"unverified", "mock-v1"}
            assert decisions and decisions[0]["method"] == "retrieval_vlm"
            assert "answer_2" in decisions[0]["evidence"], decisions[0]["evidence"]
            assert decisions[0]["candidates"]
            assert len(unit.all("raw_records")) == len(unit.all("products")) == 1
            assert not unit.all("price_history")
        report = json_value(
            {
                "mode": "LIVE",
                "fixture": "Собственные синтетические данные",
                "understanding": understanding,
                "decisions": decisions,
                "index": indexed,
                "report": service.report(identifier),
            }
        )
        Path(".tmp/reports").mkdir(parents=True, exist_ok=True)
        Path(".tmp/reports/live-contract-smoke.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        state = service.index.status()
        if state.get("collection_name"):
            service.index.request(
                "POST",
                "/collections/aliases",
                {"actions": [{"delete_alias": {"alias_name": service.index.alias}}]},
            )
            service.index.request("DELETE", "/collections/" + state["collection_name"])
