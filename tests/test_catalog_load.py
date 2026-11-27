# tests/test_catalog_load.py
"""Отдельный прогон 25 000 синтетических строк для замера SQL-пути импорта и витрины."""

import json
import os
from collections import Counter
from pathlib import Path
from statistics import median
from time import perf_counter

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.infrastructure.job_queue import JobQueue
from app.presentation.api import create_app
from app.workers.catalog import run
from tests.test_catalog_pipeline import catalog as catalog
from tests.test_catalog_pipeline import input_file


@pytest.mark.integration
def test_catalog_25000_rows(catalog, tmp_path):
    """Выполняет полный импорт через утверждённое соответствие и меряет запросы витрины."""
    if os.environ.get("CATALOG_LOAD_TEST") != "1":
        pytest.skip("Нагрузочный прогон запускается через bash scripts/test.sh --catalog-load")
    target = catalog.store.categories()[0]["id"]
    with catalog.store.transaction() as unit:
        source = unit.add("sources", name="synthetic_test", kind="price_file")
        unit.add(
            "category_source_mappings",
            source_id=source["id"],
            external_category_key="Нагрузочные кабели",
            category_id=target,
            status="approved",
            approved_by="fixture",
        )
    count = 25_000
    path = input_file(
        tmp_path,
        [
            {
                "source_key": f"load-{n}",
                "name": f"Кабель нагрузочный {n:05d}",
                "mpn": f"LOAD-{n:05d}",
                "manufacturer": f"Завод {n % 5}",
                "brand": "Тест",
                "category_path": ["Нагрузочные кабели"],
                "price": "115",
                "currency": "RUB",
            }
            for n in range(count)
        ],
    )
    start = perf_counter()
    job = catalog.upload([(path.name, path)])["job"]["id"]
    preview = perf_counter() - start
    start = perf_counter()
    catalog.start(job, "load-test")
    enqueue = perf_counter() - start
    print(f"Задание {job}: preview {preview:.2f} с, подготовка по SQL {enqueue:.2f} с", flush=True)
    assert catalog.store.claim_job(job) == job
    start = perf_counter()
    catalog.process(job)
    process = perf_counter() - start
    job_status = catalog.job(job)
    assert job_status["processed_count"] == job_status["assigned_count"] == count
    assert job_status["products_created_count"] == count
    client = TestClient(create_app(catalog))
    manufacturer = client.get("/api/v1/manufacturers").json()
    assert len(manufacturer) == 5
    scenarios = {
        "first_page": ("/api/v1/products", {"sort": "name_asc"}, count),
        "last_page": ("/api/v1/products", {"sort": "name_asc", "offset": count - 50}, count),
        "name_search": ("/api/v1/products", {"q": "нагрузочный 0004"}, 10),
        "mpn_search": ("/api/v1/products", {"q": "load24999"}, 1),
        "manufacturer": (
            "/api/v1/products",
            {"manufacturer_id": manufacturer[0]["id"]},
            count // 5,
        ),
        "category_branch": ("/api/v1/products", {"category_id": str(target)}, count),
    }
    timings = {}
    for name, (url, params, total) in scenarios.items():
        samples = []
        for _ in range(7):
            started = perf_counter()
            response = client.get(url, params=params)
            samples.append((perf_counter() - started) * 1000)
            assert response.status_code == 200 and response.json()["total"] == total
        timings[name] = {"median_ms": round(median(samples), 2), "max_ms": round(max(samples), 2)}
    start = perf_counter()
    report = catalog.report(job)
    report_time = perf_counter() - start
    assert len(report["items"]) == count
    artifact = {
        "rows": count,
        "mode": "synthetic, postgres, approved_mapping",
        "preview_seconds": round(preview, 3),
        "enqueue_seconds": round(enqueue, 3),
        "processing_seconds": round(process, 3),
        "processing_rows_per_second": round(count / process, 2),
        "report_seconds": round(report_time, 3),
        "http_timings": timings,
    }
    Path(".tmp/reports/stage04-catalog-load.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(artifact, ensure_ascii=False), flush=True)


@pytest.mark.integration
def test_identity_25000_rows(catalog, tmp_path, monkeypatch):
    """Измеряет 25 000 строк с повторами и конфликтами на PostgreSQL и настоящем AMQP."""
    if os.environ.get("CATALOG_IDENTITY_LOAD_TEST") != "1":
        pytest.skip("Нагрузке identity требуется отдельная временная очередь проекта")
    queue_name = os.environ["RABBITMQ_QUEUE"]
    assert queue_name.startswith("product-search-engine.identity-test.")
    catalog.settings.job_transport = "rabbitmq"
    catalog.queue = JobQueue(catalog.settings)
    monkeypatch.setattr("app.workers.catalog.build_catalog", lambda _: catalog)
    target = catalog.store.categories()[0]["id"]
    with catalog.store.transaction() as unit:
        source = unit.add("sources", name="synthetic_test", kind="price_file")
        unit.add(
            "category_source_mappings",
            source_id=source["id"],
            external_category_key="Стенд",
            category_id=target,
            status="approved",
            approved_by="fixture",
        )
    calls = Counter()
    real_answer = catalog.inference.answer

    def answer(step, values):
        """Отдельно считает вызовы mock inference по этапам."""
        calls[f"answer_{step}"] += 1
        return real_answer(step, values)

    monkeypatch.setattr(catalog.inference, "answer", answer)
    timings = Counter()

    @sa.event.listens_for(catalog.store.engine, "before_cursor_execute")
    def timer_start(conn, cursor, statement, parameters, context, executemany):
        """Запоминает начало индексного запроса."""
        if "CROSS JOIN LATERAL" in statement and "product_identity_keys" in statement:
            context.identity_started = perf_counter()

    @sa.event.listens_for(catalog.store.engine, "after_cursor_execute")
    def timer_end(conn, cursor, statement, parameters, context, executemany):
        """Считает время PostgreSQL и число запросов retrieval."""
        if hasattr(context, "identity_started"):
            timings["retrieval_sql_calls"] += 1
            timings["retrieval_sql_seconds"] += perf_counter() - context.identity_started

    count = 25_000
    path = input_file(
        tmp_path,
        [
            {
                "source_key": f"spot-{number}",
                "name": (
                    f"Выключатель стендовый A{number // 4:05d} IP{67 if number % 4 == 3 else 66}"
                ),
                "mpn": f"A{number // 4:05d}",
                "brand": None if number % 4 == 2 else "Стенд",
                "seller": f"Поставщик {number % 4}",
                "category_path": ["Стенд"],
                "price": "125",
                "currency": "RUB",
            }
            for number in range(count)
        ],
    )
    started = perf_counter()
    try:
        job = catalog.upload([(path.name, path)])["job"]["id"]
        catalog.start(job, "identity-load")
        prepared = perf_counter()
        print(f"Подготовлено {count} строк за {prepared - started:.2f} с", flush=True)
        assert run(catalog.settings, once=True)["mode"] == "mock"
        finished = perf_counter()
        counters = catalog.job(job)
        with catalog.store.readonly() as unit:
            counts = unit.catalog_counts()
        assert counters["processed_count"] == counters["assigned_count"] == count
        assert counts["products"] == count * 3 // 4
        assert counts["offers"] == counts["price_observations"] == count
        assert catalog.store.claim_job(job) is None
        data = {
            "rows": count,
            "inference": "mock",
            "transport": "RabbitMQ",
            "counts": counts,
            "prepared_seconds": prepared - started,
            "processing_seconds": finished - prepared,
            "requests": dict(calls),
            "index": dict(timings),
        }
        Path(".tmp/reports/identity-load.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
        print(json.dumps(data), flush=True)
    finally:
        connection = catalog.queue.connect()
        try:
            connection.channel().queue_delete(queue_name)
        finally:
            connection.close()
