# app/presentation/api.py
"""FastAPI: тонкий транспорт общих операций, durable SSE и безопасных файловых загрузок."""

import asyncio
import csv
import io
import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from app.application.taxonomy_review import TaxonomyReview
from app.catalog_bootstrap import build_catalog
from app.domain.catalog import (
    TERMINAL_JOBS,
    CatalogError,
    ConflictError,
    DependencyUnavailable,
    json_value,
)
from app.infrastructure.catalog_formats import sample_catalog
from app.presentation.dto import ItemsPage, JobView, PreviewView, TreeNodeView


class MappingInput(BaseModel):
    """Явное сопоставление полей, кодировки, листов и заголовка."""

    fields: dict[str, str] = Field(default_factory=dict)
    encoding: Literal["utf-8", "utf-8-sig", "cp1251"] = "utf-8-sig"
    delimiter: str | None = None
    sheets: list[str] | None = None
    header_row: int = Field(default=1, ge=1, le=1000)
    save_profile: bool = False


class CategoryInput(BaseModel):
    """Новая собственная категория с обязательным основанием."""

    name: str = Field(min_length=1, max_length=512)
    parent_id: UUID | None = None
    is_assignable: bool = True
    description: str | None = Field(default=None, max_length=10000)
    purpose: str | None = Field(default=None, max_length=10000)
    exclusions: str | None = Field(default=None, max_length=10000)
    reason: str = Field(min_length=1, max_length=2000)


class DecisionInput(BaseModel):
    """Оптимистическая версия и независимый выбор Product либо Category."""

    action: Literal[
        "assign_category", "confirm_product", "reject_product", "not_product", "leave_uncategorized"
    ] = "assign_category"
    expected_version: int = Field(ge=1)
    category_id: UUID | None = None
    product_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=2000)


class BulkItem(BaseModel):
    """Только явно выбранная позиция с ожидаемой версией."""

    id: UUID
    expected_version: int = Field(ge=1)


class BulkInput(BaseModel):
    """Пакетное назначение требует preview выбранного набора."""

    items: list[BulkItem] = Field(min_length=1, max_length=200)
    category_id: UUID
    reason: str = Field(min_length=1, max_length=2000)
    preview_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ChangeInput(BaseModel):
    """Предложение изменения дерева; применение выполняется отдельной командой."""

    action: Literal["keep", "remove_child", "remove_parent_reparent", "merge", "deactivate"]
    category_id: UUID
    target_category_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=2000)


class ApplyChangeInput(BaseModel):
    """Утверждение неизменившегося preview changeset."""

    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def create_app(service=None) -> FastAPI:
    """Подключает одно application service; авторизация отключена явным решением владельца."""
    service = service or build_catalog()
    app = FastAPI(
        title="Product Search Engine",
        version="0.2.0",
        description="Импорт и ручная классификация. Входные цены — исторические наблюдения.",
    )
    app.state.catalog = service
    app.mount("/static", StaticFiles(directory="app/presentation/static"), name="static")
    templates = Jinja2Templates(directory="app/presentation/templates")

    @app.exception_handler(CatalogError)
    async def catalog_error(request: Request, error: CatalogError):
        """Возвращает диагностируемый конфликт/ошибку без SQL и секретов."""
        return JSONResponse(
            {"detail": str(error)}, status_code=409 if isinstance(error, ConflictError) else 400
        )

    @app.exception_handler(DependencyUnavailable)
    async def dependency_error(request: Request, error: DependencyUnavailable):
        """Не объявляет недоступный shared dependency успешным mock-ответом."""
        return JSONResponse({"detail": str(error)}, status_code=503)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        """Скрывает параметры и credentials драйвера."""
        return JSONResponse(
            {"detail": "Операция БД откатилась; обновите данные и повторите"}, status_code=503
        )

    @app.get("/", include_in_schema=False)
    @app.get("/admin/import", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/admin/review", response_class=HTMLResponse, include_in_schema=False)
    def workspace(request: Request):
        """Отдаёт действующее рабочее место с явной маркировкой режима inference."""
        return templates.TemplateResponse(
            request=request,
            name="import.html",
            context={
                "mode": service.inference.mode,
                "upload_max_files": service.settings.upload_max_files,
                "upload_max_bytes": service.settings.upload_max_bytes,
                "upload_max_rows": service.settings.upload_max_rows,
            },
        )

    @app.get("/health/live")
    def live():
        """Проверяет HTTP event loop без тяжёлых inference вызовов."""
        return {"status": "alive", "mode": service.inference.mode}

    @app.get("/health/ready")
    def ready():
        """Проверяет SQL, согласованный индекс и транспорт заданий."""
        with service.store.transaction() as unit:
            unit.all("taxonomy_index_state")
        state = service.index.status()
        if state["status"] != "ready" or state["pending_events"]:
            return JSONResponse(
                {"status": "not_ready", "reason": "taxonomy_index", "index": state}, status_code=503
            )
        service.index.request("GET", f"/collections/{service.index.alias}")
        service.inference.check_availability()
        if service.settings.job_transport == "rabbitmq":
            connection = service.queue.connect()
            connection.close()
        return {"status": "ready", "mode": service.inference.mode, "index": state}

    @app.get("/api/v1/import-jobs", response_model=list[JobView])
    def jobs():
        """Возвращает историю заданий для восстановления после F5."""
        with service.store.transaction() as unit:
            return json_value(
                sorted(unit.all("ingest_jobs"), key=lambda j: j["created_at"], reverse=True)[:100]
            )

    @app.post("/api/v1/import-jobs", status_code=201, response_model=PreviewView)
    async def upload(
        files: Annotated[list[UploadFile], File()], options: Annotated[str, Form()] = "{}"
    ):
        """Потоково ограничивает upload до parser; staging всегда удаляется."""
        try:
            config = json.loads(options)
            if not isinstance(config, dict):
                raise ValueError
        except ValueError as error:
            raise CatalogError("options должен быть JSON-объектом") from error
        if len(files) > service.settings.upload_max_files:
            raise CatalogError("Слишком много файлов")
        temporary, inputs = [], []
        try:
            folder = Path(".tmp/temp/uploads")
            folder.mkdir(parents=True, exist_ok=True)
            for file in files:
                common = {"application/octet-stream", "text/plain"}
                types = {
                    ".json": {"application/json", "text/json"},
                    ".xml": {"application/xml", "text/xml"},
                    ".csv": {"text/csv", "application/csv", "application/vnd.ms-excel"},
                    ".xlsx": {
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "application/zip",
                    },
                }
                suffix = Path(file.filename or "").suffix.lower()
                if file.content_type and file.content_type.split(";")[0] not in common | types.get(
                    suffix, set()
                ):
                    raise CatalogError("MIME файла не соответствует поддерживаемому формату")
                path = folder / (uuid4().hex + Path(file.filename or "").suffix)
                temporary.append(path)
                count = 0
                with path.open("wb") as target:
                    while chunk := await file.read(1024 * 1024):
                        count += len(chunk)
                        if count > service.settings.upload_max_bytes:
                            raise CatalogError("Превышен лимит размера файла")
                        target.write(chunk)
                inputs.append((file.filename or "upload", path))
            return await run_in_threadpool(service.upload, inputs, config)
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)
            for file in files:
                await file.close()

    @app.get("/api/v1/import-jobs/{job_id}/preview", response_model=PreviewView)
    def preview(job_id: UUID):
        """Возвращает raw/normalized preview и mapping suggestions."""
        return service.preview(job_id)

    @app.put("/api/v1/import-jobs/{job_id}/mapping", response_model=PreviewView)
    def mapping(job_id: UUID, values: MappingInput):
        """Подтверждает mapping перед общей операцией start."""
        return service.mapping(job_id, values.model_dump(exclude_none=True))

    @app.post("/api/v1/import-jobs/{job_id}/start", status_code=202)
    def start(job_id: UUID, idempotency_key: Annotated[str, Header(min_length=1, max_length=200)]):
        """Идемпотентно ставит импорт в durable очередь."""
        return service.start(job_id, idempotency_key)

    @app.get("/api/v1/import-jobs/{job_id}", response_model=JobView)
    def job(job_id: UUID):
        """Возвращает текущий status и счётчики."""
        return service.job(job_id)

    @app.post("/api/v1/import-jobs/{job_id}/{action}", status_code=202)
    def control(job_id: UUID, action: Literal["cancel", "resume"]):
        """Сохраняет checkpoint при cancel и возобновляет технические ошибки."""
        return service.control(job_id, action)

    @app.get("/api/v1/import-jobs/{job_id}/events")
    async def events(
        request: Request,
        job_id: UUID,
        after_id: int = Query(0, ge=0),
        last_event_id: Annotated[str | None, Header()] = None,
    ):
        """Replay SSE из PostgreSQL; reconnect не запускает ingestion повторно."""
        try:
            cursor = max(after_id, int(last_event_id or 0))
        except ValueError as error:
            raise CatalogError("Неверный Last-Event-ID") from error
        service.job(job_id)

        async def stream():
            """Отправляет events с cursor и heartbeat, завершает поток после terminal job."""
            nonlocal cursor
            while not await request.is_disconnected():
                with service.store.transaction() as unit:
                    rows = unit.events(job_id, cursor)
                    state = unit.get("ingest_jobs", job_id)["status"]
                for event in rows:
                    cursor = event["event_id"]
                    payload = json.dumps(json_value(event), ensure_ascii=False)
                    yield f"id: {cursor}\ndata: {payload}\n\n"
                if not rows and state in TERMINAL_JOBS:
                    yield 'event: end\ndata: {"status":"complete"}\n\n'
                    break
                if not rows:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/import-jobs/{job_id}/items", response_model=ItemsPage)
    def items(
        job_id: UUID,
        status: str | None = None,
        q: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
    ):
        """Страницы рабочей таблицы с поиском и серверным фильтром статусов."""
        return service.items(job_id, status, q, offset, limit)

    @app.get("/api/v1/import-jobs/{job_id}/items/{item_id}")
    def item(job_id: UUID, item_id: UUID):
        """Возвращает полный trace только позиции указанного задания."""
        with service.store.transaction() as unit:
            detail = unit.item_details(item_id)
        if detail["item"]["job_id"] != job_id:
            raise CatalogError("Позиция не принадлежит заданию")
        return json_value(detail)

    @app.get("/api/v1/review/items")
    def review_items(job_id: UUID, offset: int = Query(0, ge=0)):
        """Использует ту же очередь позиций, что UI и CLI."""
        return service.items(job_id, "unresolved", "", offset, 50)

    @app.get("/api/v1/review/items/{item_id}")
    def review_detail(item_id: UUID):
        """Возвращает тот же trace, что карточка позиции import job."""
        with service.store.transaction() as unit:
            return json_value(unit.item_details(item_id))

    @app.get("/api/v1/products")
    def products(
        q: str = Query("", max_length=200),
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
    ):
        """Просмотр Product без поля current price и без сетевого обхода магазинов."""
        with service.store.transaction() as unit:
            return json_value(unit.products_page(q, offset, limit))

    @app.get("/api/v1/ingest-runs/{run_id}")
    def source_run(run_id: UUID):
        """Читает аудит конкретного источника в multi-file job."""
        with service.store.transaction() as unit:
            return json_value(unit.get("source_runs", run_id))

    @app.post("/api/v1/review/items/{item_id}/decisions")
    def decide(
        item_id: UUID,
        values: DecisionInput,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        """Сохраняет ручное решение с expected_version и основанием."""
        return service.decide(
            item_id, values.model_dump(mode="json"), "operator_unverified", idempotency_key
        )

    @app.get("/api/v1/categories/tree", response_model=list[TreeNodeView])
    def tree():
        """Возвращает существующие UUID и полные актуальные пути из SQL."""
        return json_value(service.store.categories())

    @app.post("/api/v1/review/bulk-preview")
    def bulk_preview(values: BulkInput):
        """Показывает перечень выбранных строк до пакетного назначения."""
        return service.bulk_preview(values.model_dump(mode="json", exclude_none=True))

    @app.post("/api/v1/review/bulk-decisions")
    def bulk_apply(
        values: BulkInput, idempotency_key: Annotated[str, Header(min_length=1, max_length=150)]
    ):
        """Сохраняет per-item результат и явно возвращает конфликты версий."""
        return service.bulk_apply(
            values.model_dump(mode="json", exclude_none=True),
            "operator_unverified",
            idempotency_key,
        )

    @app.post("/api/v1/categories", status_code=201)
    def category(
        values: CategoryInput, idempotency_key: Annotated[str, Header(min_length=1, max_length=200)]
    ):
        """SQL commit создаёт outbox; ответ sync_pending не имитирует подтверждение Qdrant."""
        return service.create_category(
            values.model_dump(mode="json"), "operator_unverified", idempotency_key
        )

    @app.get("/api/v1/taxonomy/index-status")
    def index_status():
        """Показывает отставание индекса и последний reconciliation."""
        return service.index.status()

    @app.get("/api/v1/taxonomy/singletons")
    def singletons():
        """Аудит актуальных одиночных ветвей без автоматической оптимизации."""
        return TaxonomyReview(service.store).singletons()

    @app.post("/api/v1/taxonomy/change-sets", status_code=201)
    def propose_change(
        values: ChangeInput, idempotency_key: Annotated[str, Header(min_length=1, max_length=150)]
    ):
        """Сохраняет предложение и scope, не меняет текущие категории."""
        return TaxonomyReview(service.store).propose(
            values.model_dump(mode="json"), "operator_unverified", idempotency_key
        )

    @app.post("/api/v1/taxonomy/change-sets/{change_id}/apply")
    def apply_change(
        change_id: UUID,
        values: ApplyChangeInput,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=150)],
    ):
        """Явно утверждает конкретный SQL preview и создаёт transactional outbox."""
        return TaxonomyReview(service.store).apply(
            change_id, values.expected_sha256, "operator_unverified", idempotency_key
        )

    @app.post("/api/v1/taxonomy/reindex", status_code=202)
    def reindex():
        """Создаёт durable signal; тяжёлую синхронизацию выполняет worker."""
        service.request_reindex()
        return {"status": "sync_pending"}

    @app.get("/api/v1/import-jobs/{job_id}/report")
    def report(job_id: UUID, format: Literal["json", "csv"] = "json"):
        """Экспортирует trace и issues; CSV защищён от формул при открытии в Excel."""
        result = service.report(job_id)
        if format == "json":
            return result
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";")
        columns = (
            "source_file",
            "sheet",
            "row",
            "raw_name",
            "product_id",
            "category_id",
            "category_path",
            "status",
            "issue_codes",
            "decision_method",
            "review_task_ids",
        )
        writer.writerow(columns)
        for row in result["items"]:
            values = [
                json.dumps(row.get(k), ensure_ascii=False)
                if isinstance(row.get(k), list)
                else str(row.get(k) or "")
                for k in columns
            ]
            writer.writerow(
                ["'" + v if v.lstrip().startswith(("=", "+", "-", "@")) else v for v in values]
            )
        return Response(
            "\ufeff" + output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="import-report.csv"'},
        )

    @app.get("/api/v1/import-jobs/{job_id}/report.json")
    def report_json(job_id: UUID):
        """Сохраняет файловый URL отчёта из обязательного addon-контракта."""
        return service.report(job_id)

    @app.get("/api/v1/import-jobs/{job_id}/report.csv")
    def report_csv(job_id: UUID):
        """Использует тот же защищённый CSV exporter."""
        return report(job_id, "csv")

    @app.get("/api/v1/import-templates/{format_name}")
    def download_template(format_name: Literal["json", "xml", "xlsx", "csv"]):
        """Отдаёт рабочий синтетический шаблон без исходных пользовательских данных."""
        payload, content_type = sample_catalog(format_name)
        return Response(
            payload,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="sample_items.{format_name}"'},
        )

    @app.get("/import-formats", response_class=HTMLResponse, include_in_schema=False)
    def formats():
        """Публикует документацию как безопасный текст без исполнения HTML из Markdown."""
        import html

        content = Path("docs/import-formats.md").read_text(encoding="utf-8")
        return HTMLResponse(
            "<html lang='ru'><meta charset='utf-8'><title>Форматы импорта</title><pre>"
            + html.escape(content)
            + "</pre></html>"
        )

    return app
