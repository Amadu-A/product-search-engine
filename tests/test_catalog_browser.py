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

from app.application.category_selection import CategorySelection, candidate_view
from app.catalog_bootstrap import build_catalog
from app.presentation.api import create_app
from app.settings import Settings
from tests.catalog_fixture import restore_categories
from tests.test_catalog_pipeline import catalog as catalog
from tests.test_catalog_pipeline import input_file, run_import

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


def test_public_catalog_filters_pagination_and_price_card(browser_workspace, tmp_path):
    """На обычном HTTP показывает ветки, фильтры и карточку с исходной ценой."""
    service, address = browser_workspace
    parent = service.create_category({"name": "Стенд", "reason": "Тест"}, "test", "catalog-root")
    child = service.create_category(
        {"name": "Кабельная продукция", "parent_id": parent["id"], "reason": "Тест"},
        "test",
        "catalog-leaf",
    )
    job = run_import(
        service,
        input_file(
            tmp_path,
            [
                {
                    "name": "Кабель стендовый",
                    "mpn": "PSE-0401",
                    "manufacturer": "Пробный завод",
                    "seller": "Пробный магазин",
                    "price": "49.50",
                    "currency": "RUB",
                    "price_date": "2024-11-15",
                    "product_url": "https://example.org/catalog-example",
                },
            ],
        ),
    )
    item = service.report(job)["items"][0]
    service.decide(
        item["id"],
        {"category_id": child["id"], "expected_version": item["processing_version"]},
        "test",
        "catalog-assignment",
    )
    with service.store.transaction() as unit:
        for n in range(26):
            unit.add("products", canonical_name=f"Изделие без категории {n:02d}")
        unit.update(
            "products",
            item["product_id"],
            attributes={
                "understanding": {
                    "attributes": [{"name": "Сечение", "value": "2,5", "unit": "мм²"}]
                }
            },
        )
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[
                "--no-sandbox",
                "--host-resolver-rules=MAP pse.test 127.0.0.1",
                "--no-proxy-server",
            ]
        )
        page = browser.new_page(viewport={"width": 1500, "height": 960})
        page.on("pageerror", lambda error: errors.append(str(error)))
        base = address.replace("127.0.0.1", "pse.test")
        page.goto(base + "/")
        assert not page.evaluate("isSecureContext")
        expect(page.locator("#catalog-total")).to_have_text("Товаров: 27")
        page.locator("#catalog-next").click()
        expect(page.locator("#catalog-page")).to_have_text("2 / 2")
        expect(page.locator("#catalog-products tr")).to_have_count(2)
        page.get_by_role("button", name="Подкатегории: Стенд").click()
        page.get_by_role("button", name="Кабельная продукция 1").click()
        expect(page.locator("#catalog-total")).to_have_text("Товаров: 1")
        page.get_by_role("button", name="Кабель стендовый").click()
        expect(page.locator("#catalog-card-title")).to_have_text("Кабель стендовый")
        expect(page.locator("#catalog-identity")).to_contain_text("Пробный завод")
        expect(page.locator("#catalog-features")).to_contain_text("Сечение")
        expect(page.locator("#catalog-links a").first).to_have_attribute(
            "href", "https://example.org/catalog-example"
        )
        expect(page.locator("#catalog-offers")).to_contain_text("Пробный магазин")
        expect(page.locator("#catalog-offers")).to_contain_text("2024-11-15")
        expect(page.locator("#catalog-offers")).to_contain_text("49,5 RUB")
        expect(page.locator("#catalog-card")).to_contain_text("Актуальная цена неизвестна")
        Path(".tmp/browser/catalog").mkdir(parents=True, exist_ok=True)
        page.screenshot(path=".tmp/browser/catalog/card.png", full_page=True)
        page.locator("#catalog-close").click()
        page.locator("#catalog-uncategorized").click()
        expect(page.locator("#catalog-total")).to_have_text("Товаров: 26")
        page.locator("#catalog-sort").select_option("name_desc")
        expect(page.locator("#catalog-products tr").first).to_contain_text("25")
        page.locator("#catalog-all").click()
        page.get_by_label("Производитель").select_option(label="Пробный завод")
        expect(page.locator("#catalog-total")).to_have_text("Товаров: 1")
        page.get_by_label("Поиск", exact=True).fill("pse0401")
        page.get_by_role("button", name="Найти").click()
        expect(page.locator("#catalog-products")).to_contain_text("PSE-0401")
        page.get_by_label("Поиск", exact=True).fill("нет-и-не-было")
        page.get_by_role("button", name="Найти").click()
        expect(page.locator("#catalog-empty")).to_be_visible()
        page.set_viewport_size({"width": 360, "height": 800})
        assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
        page.get_by_role("link", name="История").click()
        expect(page.locator("#history-view")).to_be_visible()
        assert not errors
        browser.close()


def test_review_remaining_and_archive_over_http(browser_workspace, tmp_path, monkeypatch):
    """Оператор утверждает вариант, повторяет остаток и подтверждает удаление с историей."""
    service, address = browser_workspace
    manual = service.create_category(
        {"name": "Контрольная категория", "reason": "Тест"}, "test", "c"
    )
    auto = service.create_category({"name": "Вторая категория", "reason": "Тест"}, "test", "c2")
    rows = {str(row["id"]): candidate_view(row) for row in service.store.categories()}
    retry = False

    def choose(fields, answer, emit):
        """Возвращает воспроизводимые варианты и один подтверждённый пробел в дереве."""
        if fields["name"] == "Лоток без ветки":
            return CategorySelection(reason="Для изделия нет подходящей ветки")
        if retry:
            return CategorySelection(category=rows[auto["id"]], selection_type="exact")
        return CategorySelection(
            unresolved=True,
            reason="Недостаточно сведений для выбора подкатегории",
            candidates=[rows[manual["id"]]],
            evidence={
                "answer_2": {"status": "needs_review", "compared_candidate_ids": [manual["id"]]}
            },
        )

    monkeypatch.setattr(service.classifier, "select", choose)
    source_path = input_file(
        tmp_path,
        [
            {"name": "Кабель для проверки", "price": "10"},
            {"name": "Кабель для повтора"},
            {"name": "Лоток без ветки"},
            {"name": "Каталог коллекция кабелей"},
            {"name": "Каталог коллекция выключателей"},
        ],
    )
    job = run_import(service, source_path)
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
        page.goto(address.replace("127.0.0.1", "pse.test") + "/admin/import?job=" + job)
        assert page.evaluate("isSecureContext") is False
        page.get_by_role("button", name="Кабель для проверки", exact=True).click()
        expect(page.locator("#detail-summary")).to_contain_text("Кабель")
        expect(page.locator("#detail-reason")).to_have_text(
            "Недостаточно сведений для выбора подкатегории"
        )
        expect(page.locator("#detail-technical")).not_to_have_attribute("open", "")
        expect(page.locator("#detail-raw")).not_to_be_visible()
        expect(page.get_by_text("Подтвердить Product", exact=True)).to_have_count(0)
        page.get_by_role("button", name="Утвердить: Контрольная категория").click()
        expect(page.locator("#manual-assigned-count")).to_have_text("1")
        expect(page.locator("#suggestions")).not_to_be_visible()

        retry = True
        page.get_by_role("button", name="Распределить оставшиеся").click()
        expect(page.locator("#notice")).to_contain_text("Поставлено на повторную классификацию: 1")
        assert service.store.claim_job(job) == job
        service.process(job)
        expect(page.locator("#auto-assigned-count")).to_have_text("1")
        expect(page.locator("#gap-count")).to_have_text("1")
        expect(page.locator("#not-product-count")).to_have_text("2")

        stale = page.request.post(
            f"{address}/api/v1/import-jobs/{job}/archive-not-products",
            headers={"Idempotency-Key": "stale-count"},
            data={"expected_count": 1},
        )
        assert stale.status == 409
        page.locator("#archive-not-products").click()
        page.locator("#archive-cancel").click()
        expect(page.locator("#not-product-count")).to_have_text("2")
        page.locator("#archive-not-products").click()
        page.locator("#archive-confirm").click()
        expect(page.locator("#not-product-count")).to_have_text("0")
        report = service.report(job)
        assert report["job"]["archived_not_product_count"] == 2
        assert len(report["items"]) == 5 and report["statuses"]["not_product"] == 2

        page.get_by_role("button", name="Категории", exact=True).first.click()
        page.locator("#manage-category-search").fill("Контрольная категория")
        page.locator("#all-categories button").click()
        page.locator("#archive-category-preview").click()
        expect(page.locator("#archive-category-impact")).to_contain_text("есть активные товары")
        expect(page.locator("#archive-category-confirm")).not_to_be_visible()
        page.locator("#archive-category-cancel").click()

        page.get_by_role("button", name="Товары", exact=True).click()
        product = page.locator("#products li", has_text="Кабель для проверки")
        product.get_by_role("button", name="Удалить товар").click()
        page.locator("#archive-confirm").click()
        expect(product).to_have_count(0)
        with service.store.transaction() as unit:
            assigned = next(row for row in report["items"] if row["assignment_origin"] == "manual")
            assert unit.get("products", assigned["product_id"])["status"] == "retired"
            assert unit.get("ingest_job_items", assigned["id"])["product_id"]
            assert (
                unit.all("offers", product_id=UUID(assigned["product_id"]))[0]["status"]
                == "retired"
            )
            assert (
                unit.all("classification_decisions", method="manual")[0]["evidence"]["reason"] == ""
            )

        page.get_by_role("button", name="Категории", exact=True).first.click()
        page.locator("#all-categories button").click()
        page.locator("#archive-category-preview").click()
        expect(page.locator("#archive-category-impact")).to_contain_text(
            "Исторические связи сохранятся"
        )
        page.locator("#archive-category-confirm").click()
        expect(page.locator("#all-categories li")).to_have_count(0)
        with service.store.transaction() as unit:
            assert not unit.get("categories", manual["id"])["is_active"]
            assert unit.get("products", assigned["product_id"])["category_id"] == UUID(manual["id"])
        replay = service.report(run_import(service, source_path))
        archived = next(item for item in replay["items"] if item["name"] == assigned["name"])
        assert (
            archived["status"] == "needs_review"
            and archived["product_id"] == assigned["product_id"]
        )
        assert replay["database_counts"]["products"] == report["database_counts"]["products"]
        assert not errors
        browser.close()


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
        expect(page.locator("#manual-assigned-count")).to_have_text("1")
        expect(page.locator("#auto-assigned-count")).to_have_text("0")
        expect(page.locator("#products-created-count")).to_have_text("1")
        expect(page.locator("#categorized-products-count")).to_have_text("1")
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


def test_file_chooser_drop_and_multipart_on_http(browser_workspace, tmp_path, monkeypatch):
    """Проверяет видимый выбор, drop, удаление, ошибки и настоящую передачу файлов."""
    service, address = browser_workspace
    monkeypatch.setattr(service.settings, "upload_max_bytes", 512)
    path = tmp_path / ("очень-длинное-имя-" * 5 + "каталог.json")
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "browser_review",
                "items": [{"name": "Кабель тестовый"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    gate = threading.Event()
    upload = service.upload

    def wait_for_ui(files, options):
        """Удерживает реальный запрос для проверки видимого состояния передачи."""
        if files[0][0] == path.name:
            assert gate.wait(15)
        return upload(files, options)

    monkeypatch.setattr(service, "upload", wait_for_ui)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[
                "--no-sandbox",
                "--host-resolver-rules=MAP pse.test 127.0.0.1",
                "--no-proxy-server",
            ]
        )
        page = browser.new_page(viewport={"width": 320, "height": 780})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(address.replace("127.0.0.1", "pse.test") + "/admin/import")
        assert page.evaluate("!isSecureContext")
        page.locator("#auto-start").uncheck()
        send = page.locator("#upload-button")
        expect(send).to_be_disabled()
        with page.expect_file_chooser() as selection:
            page.get_by_role("button", name="Выбрать файлы").click()
        selection.value.set_files(path)
        expect(page.locator("#selected-files")).to_contain_text(path.name)
        expect(page.locator("#selected-files")).to_contain_text("Готов к загрузке")
        expect(send).to_have_text("Загрузить и проверить (1)")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator(".header nav").evaluate("(nav) => nav.scrollWidth <= nav.clientWidth")
        Path(".tmp/browser").mkdir(parents=True, exist_ok=True)
        page.screenshot(path=".tmp/browser/stage024-upload-mobile.png", full_page=True)
        page.get_by_role("button", name="Удалить " + path.name).click()
        expect(send).to_be_disabled()

        drop = page.evaluate_handle("""() => {
          const data = new DataTransfer();
          data.items.add(new File(['{}'], 'format.txt'));
          data.items.add(new File(['x'.repeat(513)], 'large.json'));
          data.items.add(new File(['{'], 'broken.json', {type: 'application/json'}));
          return data;
        }""")
        page.locator("#drop-zone").dispatch_event("drop", {"dataTransfer": drop})
        expect(send).to_have_text("Загрузить и проверить (3)")
        expect(send).to_be_disabled()
        expect(page.locator("#selected-files")).to_contain_text("Формат не поддерживается")
        expect(page.locator("#selected-files")).to_contain_text("Превышен лимит")
        for name in ("format.txt", "large.json"):
            page.get_by_role("button", name="Удалить " + name).click()
        send.click()
        expect(page.locator("#upload-status")).to_contain_text("JSON")
        expect(page.locator("#selected-files")).to_contain_text("Проверка не завершена")
        page.get_by_role("button", name="Удалить broken.json").click()

        with page.expect_file_chooser() as selection:
            page.get_by_role("button", name="Выбрать файлы").click()
        selection.value.set_files(path)
        dropped_json = page.evaluate_handle("""() => {
          const data = new DataTransfer();
          data.items.add(new File([JSON.stringify({
            schema_version: 1, source: 'browser_drop', items: [{name: 'Провод тестовый'}],
          })], 'drop.json', {type: 'application/json'}));
          return data;
        }""")
        page.locator("#drop-zone").dispatch_event("drop", {"dataTransfer": dropped_json})
        expect(send).to_have_text("Загрузить и проверить (2)")
        with page.expect_request("**/api/v1/import-jobs") as posted:
            send.click()
        try:
            assert posted.value.method == "POST"
            assert posted.value.headers["content-type"].startswith("multipart/form-data; boundary=")
            expect(page.locator("#upload-progress")).to_be_visible()
            expect(send).to_be_disabled()
        finally:
            gate.set()
        expect(page.locator("#preview-files")).to_contain_text(path.name)
        expect(page.locator("#preview-files")).to_contain_text("drop.json")
        expect(page.locator("#upload-status")).to_have_text("Файлы проверены")
        expect(send).to_be_disabled()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
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
            expect(page.locator("#manual-assigned-count")).to_have_text("1")
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
                first_item = next(r for r in repeated if r["row_number"] == 1)
                assert first_item["status"] == "assigned"
                assert first_item["category_id"] == category["id"]
            expect(page.locator("#manual-assigned-count")).to_have_text("1")
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
