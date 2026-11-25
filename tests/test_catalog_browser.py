# tests/test_catalog_browser.py
"""Browser E2E: PostgreSQL/Qdrant; MOCK и отдельно разрешённый LIVE inference."""

import hashlib
import json
import socket
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import uvicorn
from openpyxl import Workbook
from playwright.sync_api import expect, sync_playwright

from app.catalog_bootstrap import build_catalog
from app.presentation.api import create_app
from app.settings import Settings
from tests.catalog_fixture import restore_categories
from tests.test_catalog_pipeline import catalog as catalog

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


@pytest.fixture
def browser_workspace(catalog):
    """Поднимает настоящий HTTP сервер над общей синтетической PostgreSQL fixture."""
    catalog.index.sync()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    address = "http://127.0.0.1:" + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(create_app(catalog), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield catalog, address
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_browser_mapping_sheets_bulk_and_safe_text(browser_workspace, tmp_path):
    """Проверяет CSV mapping, лист XLSX, XML, bulk preview, XSS и мобильную ширину."""
    service, address = browser_workspace
    csv_path = tmp_path / "mapped.csv"
    csv_path.write_text(
        'Описание;Стоимость\n"Кабель <img src=x onerror=window.pwned=1>";12.50\n', encoding="utf-8"
    )
    book = Workbook()
    book.active.title = "Первый"
    book.active.append(["Описание", "Стоимость"])
    book.active.append(["Не выбранная строка", "1.00"])
    sheet = book.create_sheet("Второй")
    sheet.append(["Описание", "Стоимость"])
    sheet.append(["Кабель второй тестовый", "2.00"])
    xlsx_path = tmp_path / "sheets.xlsx"
    book.save(xlsx_path)
    book.close()
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[
                "--no-sandbox",
                "--host-resolver-rules=MAP pse.test 127.0.0.1",
                "--no-proxy-server",
            ]
        )
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(address.replace("127.0.0.1", "pse.test") + "/admin/import")
        assert page.evaluate("isSecureContext") is False
        page.locator("#auto-start").uncheck()
        page.locator("#files").set_input_files(csv_path)
        page.locator("#upload-button").click()
        page.wait_for_url("**?job=*")
        identifier = page.url.split("job=")[1]
        page.locator('[data-field="name"]').select_option("Описание")
        page.locator('[data-field="price"]').select_option("Стоимость")
        page.locator("#mapping-button").click()
        expect(page.locator("#start-button")).to_be_enabled()
        page.locator("#start-button").click()
        deadline = time.monotonic() + 15
        while not service.store.claim_job(identifier) and time.monotonic() < deadline:
            time.sleep(0.05)
        service.process(identifier)
        page.reload()
        expect(page.locator("#progress-label")).to_have_text("Обработано 1 / 1", timeout=10000)
        assert page.evaluate("window.pwned") is None
        assert page.locator("#items img").count() == 0
        page.locator("#items button").first.click()
        page.locator("#category-tree button:not([disabled])").first.click()
        page.locator("#decision-reason").fill("Пакет проверен браузером")
        page.locator("#items input[type=checkbox]").first.check()
        page.locator("#bulk-preview").click()
        expect(page.locator("#bulk-dialog")).to_be_visible()
        expect(page.locator("#bulk-items li")).to_have_count(1)
        page.locator("#bulk-confirm").click()
        expect(page.locator("#assigned-count")).to_have_text("1")
        page.locator("#files").set_input_files(xlsx_path)
        page.locator("#upload-button").click()
        page.wait_for_function(
            "old => new URL(location).searchParams.get('job') !== old", arg=identifier
        )
        second = page.url.split("job=")[1]
        page.locator("#sheets").fill("Второй")
        page.locator('[data-field="name"]').select_option("Описание")
        page.locator('[data-field="price"]').select_option("Стоимость")
        page.locator("#mapping-button").click()
        page.locator("#start-button").click()
        deadline = time.monotonic() + 15
        while not service.store.claim_job(second) and time.monotonic() < deadline:
            time.sleep(0.05)
        service.process(second)
        page.reload()
        expect(page.locator("#items")).to_contain_text("Кабель второй тестовый", timeout=10000)
        expect(page.locator("#items")).not_to_contain_text("Не выбранная строка")
        response = page.request.get(address + "/api/v1/import-templates/xml")
        page.locator("#auto-start").uncheck()
        page.locator("#files").set_input_files(
            {"name": "sample.xml", "mimeType": "application/xml", "buffer": response.body()}
        )
        page.locator("#upload-button").click()
        page.wait_for_function(
            "old => new URL(location).searchParams.get('job') !== old", arg=second
        )
        third = page.url.split("job=")[1]
        page.locator("#start-button").click()
        deadline = time.monotonic() + 15
        while not service.store.claim_job(third) and time.monotonic() < deadline:
            time.sleep(0.05)
        service.process(third)
        page.reload()
        expect(page.locator("#progress-label")).to_have_text("Обработано 1 / 1", timeout=10000)
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('.mobile-tabs [data-mobile-tab="file"]').click()
        expect(page.locator("#upload-form")).to_be_visible()
        expect(page.locator(".progress")).not_to_be_visible()
        page.locator('.mobile-tabs [data-mobile-tab="processing"]').click()
        expect(page.locator("#progress-label")).to_be_visible()
        page.reload()
        expect(page.locator("#progress-label")).to_be_visible()
        page.locator('.mobile-tabs [data-mobile-tab="review"]').click()
        expect(page.locator("#items")).to_be_visible()
        page.locator("#items button").first.click()
        page.locator('.mobile-tabs [data-mobile-tab="categories"]').click()
        expect(page.locator("#category-tree")).to_be_visible()
        page.locator("#category-tree button:not([disabled])").first.click()
        page.locator("#decision-reason").fill("Проверка назначения с мобильного экрана")
        page.locator("#assign-button").click()
        expect(page.locator("#notice")).to_contain_text("Решение сохранено в PostgreSQL")
        assert service.job(third)["assigned_count"] == 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        Path(".tmp/browser/formats").mkdir(parents=True, exist_ok=True)
        page.screenshot(path=".tmp/browser/formats/mobile.png", full_page=True)
        assert not errors
        browser.close()


def test_aitim200_browser_sql_qdrant(db_engine, tmp_path):
    """Загружает 200 строк браузером, переживает F5, создаёт категорию, назначает и повторяет."""
    source = Path("examples/aitim_200_raw.json")
    if not source.exists() or not Path("data/fixtures/categories_fixture.dump").exists():
        if Settings().live_smoke_enabled:
            pytest.fail("LIVE требует настоящий AITIM-200 и approved fixture dump")
        pytest.skip("Для полной приёмки нужны пользовательские AITIM-200 и approved dump")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    assert checksum == "a160f659c44bab5f36623d629a9740882ccb2ec5b7f8683bb06ae5b4fded8f28"
    restored = restore_categories(db_engine)
    assert restored["categories"] == 2540
    mode = "live" if Settings().live_smoke_enabled else "mock"
    settings = Settings(
        inference_mode=mode,
        job_transport="postgres",
        upload_dir=tmp_path / "uploads",
        qdrant_collection_alias="product_taxonomy_categories_test_" + uuid4().hex,
    )
    service = build_catalog(settings, db_engine)
    assert len(service.store.categories()) == 2540
    before_ids = {str(r["id"]) for r in service.store.categories()}
    assert service.index.sync()["points"] == 2540
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(service), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    artifacts = Path(".tmp/browser/aitim200-" + mode)
    artifacts.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1600, "height": 1100})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}/admin/import")
            if mode == "mock":
                expect(page.locator(".mode")).to_contain_text("MOCK")
            else:
                expect(page.locator(".mode")).to_have_count(0)
            page.locator("#files").set_input_files(source)
            page.get_by_role("button", name="Загрузить и проверить").click()
            page.wait_for_url("**?job=*")
            identifier = page.url.split("job=")[1]
            deadline = time.monotonic() + 30
            claimed = None
            while not claimed and time.monotonic() < deadline:
                claimed = service.store.claim_job(identifier)
                time.sleep(0.05)
            assert claimed
            worker = threading.Thread(target=service.process, args=(claimed,), daemon=True)
            worker.start()
            page.reload()
            worker.join(timeout=240 if mode == "mock" else 3600)
            assert not worker.is_alive()
            page.reload()
            expect(page.locator("#progress-label")).to_have_text(
                "Обработано 200 / 200", timeout=15000
            )
            expect(page.locator("#items-total")).to_have_text("200 позиций")
            page.screenshot(path=str(artifacts / "01-completed.png"), full_page=True)
            page.locator("#items button").first.click()
            page.locator("#new-category").click()
            page.locator("#category-name").fill("Тестовая категория E2E")
            page.locator("#category-reason").fill("Изолированная ручная проверка")
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/api/v1/categories")
                    and response.request.method == "POST"
                )
            ) as created:
                page.locator("#category-form").get_by_role(
                    "button", name="Создать категорию", exact=True
                ).click()
            assert created.value.status == 201, created.value.text()
            try:
                expect(page.locator("#category-dialog")).not_to_be_visible(timeout=15000)
            except AssertionError as error:
                page.screenshot(path=str(artifacts / "03-create-failure.png"), full_page=True)
                raise AssertionError(page.locator("#notice").inner_text() + str(errors)) from error
            page.locator("#decision-reason").fill("Проверено оператором E2E")
            page.locator("#assign-button").click()
            expect(page.locator("#assigned-count")).to_have_text("1")
            indexed = service.index.sync()
            assert indexed["points"] == 2541
            category = next(
                r for r in service.store.categories() if r["name"] == "Тестовая категория E2E"
            )
            points = service.index.scroll(indexed["collection_name"])
            assert str(category["id"]) in points
            page.screenshot(path=str(artifacts / "02-manual-assigned.png"), full_page=True)
            first_report = service.report(identifier)
            with service.store.transaction() as unit:
                raw_count = len(unit.all("raw_records"))
                products_count = len(unit.all("products"))
                observations_count = len(unit.all("price_observations"))
                assert raw_count == 200 and observations_count == 200
                assert not unit.all("price_history")
            page.locator("#files").set_input_files(source)
            page.get_by_role("button", name="Загрузить и проверить").click()
            page.wait_for_function(
                "old => new URL(location).searchParams.get('job') !== old", arg=identifier
            )
            second_id = page.url.split("job=")[1]
            deadline = time.monotonic() + 30
            claimed = None
            while not claimed and time.monotonic() < deadline:
                claimed = service.store.claim_job(second_id)
                time.sleep(0.05)
            assert claimed
            service.process(claimed)
            page.reload()
            expect(page.locator("#progress-label")).to_have_text(
                "Обработано 200 / 200", timeout=15000
            )
            with service.store.transaction() as unit:
                assert len(unit.all("raw_records")) == raw_count
                assert len(unit.all("products")) == products_count
                assert len(unit.all("price_observations")) == observations_count
                repeated = unit.all("ingest_job_items", job_id=UUID(second_id))
                assert sum(r["status"] == "assigned" for r in repeated) == 1
            assert before_ids <= {str(r["id"]) for r in service.store.categories()}
            assert not errors, errors
            report = {
                "mode": mode.upper(),
                "browser": "Chromium",
                "source": str(source),
                "restored": restored,
                "first": first_report,
                "repeated": service.report(second_id),
                "raw_records": raw_count,
                "products": products_count,
                "price_observations": observations_count,
                "qdrant_points": indexed["points"],
                "javascript_errors": errors,
            }
            (artifacts / "acceptance.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        state = service.index.status()
        if state.get("collection_name"):
            service.index.request(
                "POST",
                "/collections/aliases",
                {"actions": [{"delete_alias": {"alias_name": service.index.alias}}]},
            )
            service.index.request("DELETE", "/collections/" + state["collection_name"])
