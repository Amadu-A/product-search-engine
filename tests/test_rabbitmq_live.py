# tests/test_rabbitmq_live.py
"""Отдельный настоящий AMQP smoke; inference этого транспортного теста явно MOCK."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.catalog_bootstrap import build_catalog
from app.settings import Settings
from app.workers.catalog import run
from tests.test_catalog_pipeline import input_file
from tests.test_postgres import importer

pytestmark = [pytest.mark.integration, pytest.mark.live]


def test_shared_amqp_worker_claim_recovery(db_engine, snapshot, tmp_path, monkeypatch):
    """Проверяет publisher confirm, настоящий worker, повтор уведомления и recovery lease."""
    baseline = Settings()
    if not baseline.live_smoke_enabled or baseline.job_transport != "rabbitmq":
        pytest.skip("Отдельный AMQP LIVE smoke требует project test credentials и Rabbit transport")
    smoke_queue = baseline.rabbitmq_queue.startswith("product-search-engine.stage04.smoke.")
    if smoke_queue:
        assert baseline.rabbitmq_user == "product_search_engine_app"
        assert baseline.rabbitmq_vhost == "product-search-engine"
        assert UUID(baseline.rabbitmq_queue.rsplit(".", 1)[-1])
    else:
        assert baseline.rabbitmq_user.startswith("product_search_engine_stage02_")
        assert baseline.rabbitmq_vhost.startswith("product-search-engine-stage02-")
    importer(db_engine).execute(snapshot, apply=True)
    settings = Settings(
        inference_mode="mock",
        job_transport="rabbitmq",
        upload_dir=tmp_path / "uploads",
        qdrant_collection_alias="product_taxonomy_categories_amqp_" + uuid4().hex,
        worker_lease_seconds=60,
    )
    service = build_catalog(settings, db_engine)
    monkeypatch.setattr("app.workers.catalog.build_catalog", lambda value: service)
    try:
        assert service.queue.check() == "project_roundtrip_ok"
        path = input_file(tmp_path, [{"name": "Кабель тестовый", "price": "12.50"}])
        first = service.upload([(path.name, path)])["job"]["id"]
        service.start(first, "amqp-first")
        started, release = threading.Event(), threading.Event()
        original_process = service._process

        def delayed_process(identifier):
            """Держит реальный worker дольше heartbeat транспорта и его SQL lease."""
            if identifier == first:
                started.set()
                assert release.wait(85)
            original_process(identifier)

        monkeypatch.setattr(service, "_process", delayed_process)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run, settings, once=True)
            try:
                assert started.wait(15)
                hold_start = time.monotonic()
                service.queue.publish(first)
                while time.monotonic() - hold_start < 62:
                    time.sleep(1)
                long_job_seconds = time.monotonic() - hold_start
                assert service.store.recover(settings.worker_lease_seconds) == 0
                assert service.job(first)["status"] == "running"
                assert service.store.claim_job(first) is None
            finally:
                release.set()
            result = future.result(timeout=20)
        assert result["mode"] == "mock"
        assert service.job(first)["processed_count"] == 1
        duplicate_total = 0
        while duplicate := service.queue.receive():
            connection, (channel, tag), identifier = duplicate
            try:
                assert identifier == first and service.store.claim_job(identifier) is None
                channel.basic_ack(tag)
                duplicate_total += 1
            finally:
                connection.close()
        assert duplicate_total >= 1
        second = service.upload([(path.name, path)])["job"]["id"]
        service.start(second, "amqp-second")
        assert service.store.claim_job(second) == second
        with service.store.transaction() as unit:
            table = unit.table("ingest_jobs")
            unit.connection.execute(
                table.update()
                .where(table.c.id == UUID(second))
                .values(
                    updated_at=datetime.now(UTC)
                    - timedelta(seconds=settings.worker_lease_seconds + 1)
                )
            )
        assert service.store.recover(settings.worker_lease_seconds) == 1
        run(settings, once=True)
        report = service.report(second)
        assert report["job"]["processed_count"] == 1
        assert report["database_counts"]["raw_records"] == 1
        assert report["database_counts"]["products"] == 1
        assert report["database_counts"]["price_observations"] == 1
        artifact = {
            "transport": "LIVE shared RabbitMQ",
            "inference": "MOCK (не проверка модели)",
            "publisher_confirm": True,
            "duplicate_notification": "no_second_claim",
            "lease_recovery": "processed",
            "long_job_seconds": round(long_job_seconds, 2),
            "report": report,
        }
        Path(".tmp/reports/amqp-live-smoke.json").write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        if smoke_queue:
            connection = service.queue.connect()
            try:
                connection.channel().queue_delete(settings.rabbitmq_queue)
            finally:
                connection.close()
        state = service.index.status()
        if state.get("collection_name"):
            service.index.request(
                "POST",
                "/collections/aliases",
                {"actions": [{"delete_alias": {"alias_name": service.index.alias}}]},
            )
            service.index.request("DELETE", "/collections/" + state["collection_name"])
