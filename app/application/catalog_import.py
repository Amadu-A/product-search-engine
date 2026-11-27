# app/application/catalog_import.py
"""Общие use-case загрузки, обработки и ручных решений для HTTP, worker и CLI."""

import hashlib
import shutil
import threading
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.application.catalog_ports import (
    CatalogStore,
    CategorySearch,
    FileParser,
    JobQueue,
    Understanding,
)
from app.application.category_selection import (
    CategoryClassifier,
    candidate_view,
    suggested_category_ids,
)
from app.domain.catalog import (
    NORMALIZER_VERSION,
    TERMINAL_ITEMS,
    TERMINAL_JOBS,
    CatalogError,
    ConflictError,
    DependencyUnavailable,
    InputRow,
    InvalidModelOutput,
    approved_category_exclusion,
    evidence_bundle,
    fingerprint,
    is_bare_identifier,
    item_counters,
    json_value,
    normalize_row,
    normalize_text,
    normalized_key,
    raw_type_understanding,
)
from app.domain.product_identity import claim_value, identity_profile


class CatalogImport:
    """Один pipeline независимо от вызывающего транспорта; каждый checkpoint durable."""

    def __init__(
        self,
        store: CatalogStore,
        parser: FileParser,
        inference: Understanding,
        index: CategorySearch,
        queue: JobQueue,
        settings,
    ):
        """Внедряет порты без создания соединений в domain/application."""
        self.store, self.parser, self.inference = store, parser, inference
        self.index, self.queue, self.settings = index, queue, settings
        self.classifier = CategoryClassifier(store, index, inference, settings.taxonomy_max_depth)

    def upload(
        self,
        files: list[tuple[str, Path]],
        options: dict | None = None,
        actor="operator_unverified",
    ) -> dict:
        """Проверяет все файлы перед durable job; исходные байты копируются без изменения."""
        options = options or {}
        if not files or len(files) > self.settings.upload_max_files:
            raise CatalogError("Выберите от 1 до 10 файлов")
        parsed_files = [
            (name, path, self.parser.parse(path, name, options)) for name, path in files
        ]
        if sum(len(parsed.rows) for _, _, parsed in parsed_files) > self.settings.upload_max_rows:
            raise CatalogError("Суммарное число строк превышает лимит задания")
        job_id = uuid4()
        folder = self.settings.upload_dir / str(job_id)
        folder.mkdir(parents=True, exist_ok=False)
        try:
            with self.store.transaction() as unit:
                unit.add("ingest_jobs", id=job_id, created_by=actor, status="preview_ready")
                for name, path, parsed in parsed_files:
                    file_id = uuid4()
                    target = folder / (str(file_id) + Path(name).suffix.lower())
                    shutil.copyfile(path, target)
                    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
                    preview = self.preview_data(parsed)
                    unit.add(
                        "ingest_job_files",
                        id=file_id,
                        job_id=job_id,
                        original_filename=Path(name.replace("\\", "/")).name,
                        content_sha256=checksum,
                        byte_size=target.stat().st_size,
                        format=target.suffix[1:],
                        profile=parsed.profile,
                        storage_key=str(target),
                        mapping=options,
                        preview=preview,
                    )
                unit.event(
                    job_id,
                    "job.preview_ready",
                    "Файлы проверены; предварительный просмотр готов",
                    data={"mode": self.inference.mode},
                )
        except Exception:
            for path in folder.iterdir():
                path.unlink()
            folder.rmdir()
            raise
        return self.preview(job_id)

    def preview_data(self, parsed) -> dict:
        """Показывает raw и derived рядом; строки preview не объявляются импортированными."""
        return json_value(
            {
                "profile": parsed.profile,
                "source": parsed.source_name,
                "rows_count": len(parsed.rows),
                "columns": parsed.columns,
                "sheets": parsed.sheets,
                "mapping": parsed.mapping,
                "requires_mapping": parsed.requires_mapping,
                "warnings": parsed.warnings,
                "source_metadata": parsed.metadata,
                "rows": [
                    {
                        "raw": row.raw,
                        "normalized": normalize_row(row),
                        "row_number": row.row_number,
                        "sheet_name": row.sheet_name,
                    }
                    for row in parsed.rows[: self.settings.upload_preview_rows]
                ],
            }
        )

    def preview(self, job_id) -> dict:
        """Возвращает сохранённый preview и разрешённость явного start."""
        with self.store.transaction() as unit:
            job = unit.get("ingest_jobs", job_id)
            files = unit.all("ingest_job_files", job_id=job["id"])
        return json_value(
            {
                "job": job,
                "files": [{k: v for k, v in f.items() if k != "storage_key"} for f in files],
                "can_start": bool(files)
                and all(not f["preview"]["requires_mapping"] for f in files),
                "mode": self.inference.mode,
            }
        )

    def mapping(self, job_id, options: dict, actor="operator_unverified") -> dict:
        """Изменяет mapping только до старта; сохранённый профиль создаётся явно."""
        with self.store.transaction() as unit:
            job = unit.get("ingest_jobs", job_id, lock=True)
            if job["status"] != "preview_ready":
                raise ConflictError("Сопоставление нельзя менять после начала импорта")
            for file in unit.all("ingest_job_files", job_id=job["id"]):
                new_options = {**(file["mapping"] or {}), **options, "confirmed": True}
                parsed = self.read_file(file, new_options)
                unit.update(
                    "ingest_job_files",
                    file["id"],
                    mapping=new_options,
                    profile=parsed.profile,
                    preview=self.preview_data(parsed),
                )
                if options.get("save_profile"):
                    unit.unique(
                        "import_mapping_profiles",
                        {
                            "source_name": parsed.source_name,
                            "filename_pattern": file["original_filename"],
                            "format": file["format"],
                        },
                        mapping=new_options,
                        created_by=actor,
                    )
            unit.update("ingest_jobs", job["id"], mapping_version=job["mapping_version"] + 1)
            unit.event(job["id"], "mapping.confirmed", "Сопоставление полей подтверждено")
        return self.preview(job_id)

    def read_file(self, file: dict, options=None):
        """Проверяет checksum и удерживает локальный storage path внутри upload_dir."""
        path = Path(file["storage_key"]).resolve()
        if not path.is_relative_to(self.settings.upload_dir.resolve()) or not path.is_file():
            raise CatalogError("Исходный файл недоступен; повторно загрузите его")
        if hashlib.sha256(path.read_bytes()).hexdigest() != file["content_sha256"]:
            raise CatalogError("Контрольная сумма исходного файла изменилась")
        return self.parser.parse(
            path, file["original_filename"], options if options is not None else file["mapping"]
        )

    def start(self, job_id, key: str, actor="operator_unverified") -> dict:
        """Создаёт Raw/Normalized/items транзакционно; повтор start не создаёт дубликатов."""
        request = {"operation": "start", "job_id": str(job_id)}
        with self.store.transaction() as unit:
            cached = unit.command(actor, key, request)
            if cached:
                return cached
            job = unit.get("ingest_jobs", job_id, lock=True)
            if job["status"] != "preview_ready":
                return json_value(job)
            files = unit.all("ingest_job_files", job_id=job["id"])
            total = 0
            for file in files:
                parsed = self.read_file(file)
                if parsed.requires_mapping:
                    raise CatalogError("Подтвердите сопоставление полей/выбор листов")
                unit.command(
                    "source_lock", fingerprint(parsed.source_name), {"source": parsed.source_name}
                )
                sources = unit.all("sources", name=parsed.source_name)
                source = (
                    sources[0]
                    if sources
                    else unit.add(
                        "sources",
                        name=parsed.source_name,
                        kind="archive" if parsed.profile == "aitim_archive_v1" else "price_file",
                    )
                )
                run = unit.add(
                    "source_runs",
                    source_id=source["id"],
                    input_sha256=file["content_sha256"],
                    status="pending",
                )
                unit.connection_insert_run(job["id"], file["id"], run["id"])
                items = []
                version = NORMALIZER_VERSION + ":" + fingerprint(parsed.mapping)[:12]
                for row in parsed.rows:
                    normalized = normalize_row(row)
                    fields = normalized["fields"]
                    payload_hash = fingerprint(row.raw)
                    raw = unit.unique(
                        "raw_records",
                        {
                            "source_id": source["id"],
                            "source_key": str(fields["source_key"] or payload_hash),
                            "payload_sha256": payload_hash,
                        },
                        first_run_id=run["id"],
                        payload=row.raw,
                        origin_url=fields["product_url"],
                        collected_at=datetime.now(UTC),
                        source_reported_date=date.fromisoformat(fields["price_date"])
                        if fields["price_date"]
                        else None,
                    )
                    norm = unit.unique(
                        "normalized_records",
                        {"raw_record_id": raw["id"], "normalizer_version": version},
                        normalized_name=fields["name"],
                        brand_candidate=fields["brand"],
                        mpn_candidate=fields["mpn"],
                        mpn_candidate_origin="unknown" if fields["mpn"] else None,
                        unit_candidate=fields["unit"],
                        attributes={
                            "fields": fields,
                            "raw_name": row.fields.get("name"),
                            "locator": {
                                "file_id": str(file["id"]),
                                "row_number": row.row_number,
                                "sheet_name": row.sheet_name,
                            },
                        },
                        issues=normalized["issues"],
                        status=normalized["status"],
                    )
                    item = dict(
                        id=uuid4(),
                        job_id=job["id"],
                        file_id=file["id"],
                        item_position=row.position,
                        row_number=row.row_number,
                        sheet_name=row.sheet_name,
                        raw_record_id=raw["id"],
                        normalized_record_id=norm["id"],
                        status="invalid_record" if norm["status"] == "invalid" else "pending",
                        issue_codes=[i["code"] for i in norm["issues"]],
                    )
                    self.observation(unit, source, raw, norm, row)
                    items.append(item)
                    if len(items) == 250 or item["status"] == "invalid_record":
                        unit.add_items(items)
                        items.clear()
                    if item["status"] == "invalid_record":
                        unit.event(
                            job["id"],
                            "item.invalid_record",
                            "Строка сохранена с ошибками",
                            item_id=item["id"],
                            level="warning",
                        )
                unit.add_items(items)
                total += len(parsed.rows)
            unit.update("ingest_jobs", job["id"], status="queued")
            unit.event(
                job["id"], "job.queued", "Импорт поставлен в очередь", data={"total_count": total}
            )
            response = {"id": str(job["id"]), "status": "queued", "total_count": total}
            unit.save_command(actor, key, request, response)
        self.notify_worker(job_id)
        return response

    def notify_worker(self, job_id) -> None:
        """Задание уже сохранено. При сбое доставки worker повторит публикацию."""
        try:
            self.queue.publish(str(job_id))
        except DependencyUnavailable:
            with self.store.transaction() as unit:
                unit.event(
                    UUID(str(job_id)),
                    "queue.unavailable",
                    "Задание в очереди PostgreSQL. Доставить уведомление сейчас не удалось.",
                    level="warning",
                )

    def observation(self, unit, source: dict, raw: dict, norm: dict, row: InputRow) -> None:
        """Сохраняет историческое наблюдение без проверки магазина/валюты/актуальности."""
        fields = norm["attributes"]["fields"]
        if row.fields.get("price") is None and not (
            fields.get("product_url") or fields.get("seller")
        ):
            return
        seller = None
        if fields.get("seller"):
            unit.command(
                "seller_lock",
                fingerprint([source["id"], fields["seller"], fields["seller_inn"]]),
                {},
            )
            sellers = unit.all("sellers", legal_name=fields["seller"], inn=fields["seller_inn"])
            seller = (
                sellers[0]
                if sellers
                else unit.add(
                    "sellers",
                    legal_name=fields["seller"],
                    normalized_name=normalized_key(fields["seller"]),
                    inn=fields["seller_inn"],
                )
            )
        offer_key = raw["source_key"]
        if seller:
            offer_key += ":seller:" + fingerprint([fields["seller"], fields["seller_inn"]])[:24]
        offer = unit.unique(
            "offers",
            {"source_id": source["id"], "source_offer_key": offer_key},
            seller_id=seller["id"] if seller else None,
            origin_url=fields["product_url"],
            sale_unit=fields["unit"],
            last_seen_at=datetime.now(UTC),
        )
        unit.unique(
            "price_observations",
            {"offer_id": offer["id"], "raw_record_id": raw["id"]},
            raw_price=str(row.fields["price"]) if row.fields.get("price") is not None else None,
            amount=fields["price"],
            currency=fields["currency"],
            sale_unit=fields["unit"],
            source_reported_date=raw["source_reported_date"],
            observed_at=raw["collected_at"],
            quality_status="invalid"
            if any(i["code"] == "invalid_price" for i in norm["issues"])
            else "unverified",
            issues=norm["issues"],
        )

    def source_status(self, unit, job_id, status: str) -> None:
        """Фиксирует SourceRun каждого файла с его собственными counters, отдельно от job."""
        for link in unit.all("ingest_job_source_runs", job_id=UUID(str(job_id))):
            items = unit.all("ingest_job_items", job_id=UUID(str(job_id)), file_id=link["file_id"])
            unit.update(
                "source_runs",
                link["source_run_id"],
                status=status,
                finished_at=datetime.now(UTC)
                if status in {"completed", "cancelled", "failed"}
                else None,
                counters=item_counters(items),
            )

    def process(self, job_id: str) -> None:
        """Поддерживает lease во время ограниченных сетевых вызовов и сохраняет checkpoint."""
        stop = threading.Event()
        thread = threading.Thread(target=self.keep_alive, args=(job_id, stop), daemon=True)
        thread.start()
        try:
            self._process(job_id)
        finally:
            stop.set()
            thread.join(timeout=5)

    def keep_alive(self, job_id: str, stop: threading.Event) -> None:
        """Обновляет lease независимо от времени ответа VLM; raw/progress не меняет."""
        while not stop.wait(10):
            with self.store.transaction() as unit:
                unit.update("ingest_jobs", job_id)

    def _process(self, job_id: str) -> None:
        """Обрабатывает durable строки; exception одной позиции не уничтожает соседние."""
        with self.store.transaction() as unit:
            job = unit.get("ingest_jobs", job_id)
            self.source_status(unit, job_id, "running")
            items = sorted(
                unit.all("ingest_job_items", job_id=job["id"]),
                key=lambda i: (str(i["file_id"]), i["item_position"]),
            )
        for item in items:
            with self.store.transaction() as unit:
                current_job = unit.get("ingest_jobs", job_id)
                if current_job["cancel_requested"]:
                    unit.update(
                        "ingest_jobs", job_id, status="canceled", finished_at=datetime.now(UTC)
                    )
                    self.source_status(unit, job_id, "cancelled")
                    unit.event(
                        current_job["id"], "job.canceled", "Импорт остановлен; checkpoint сохранён"
                    )
                    return
                current = unit.get("ingest_job_items", item["id"])
            if current["status"] in TERMINAL_ITEMS:
                continue
            try:
                with self.store.record_lock(current["normalized_record_id"]):
                    with self.store.transaction() as unit:
                        current = unit.get("ingest_job_items", item["id"])
                    if current["status"] not in TERMINAL_ITEMS:
                        self.process_item(current)
            except (CatalogError, DependencyUnavailable) as error:
                issue_code = (
                    "invalid_output"
                    if isinstance(error, InvalidModelOutput)
                    else "inference_unavailable"
                    if isinstance(error, DependencyUnavailable)
                    else "taxonomy_changed"
                )
                with self.store.transaction() as unit:
                    fresh = unit.get("ingest_job_items", item["id"], lock=True)
                    if fresh["processing_version"] != current["processing_version"]:
                        continue
                    unit.update(
                        "ingest_job_items",
                        item["id"],
                        status="needs_review",
                        issue_codes=sorted(
                            (set(current["issue_codes"]) - {"reclassify_requested"}) | {issue_code}
                        ),
                    )
                    unit.event(
                        UUID(job_id),
                        "item.needs_review",
                        str(error)[:250],
                        item_id=item["id"],
                        level="warning",
                    )
            except Exception as error:
                with self.store.transaction() as unit:
                    fresh = unit.get("ingest_job_items", item["id"], lock=True)
                    if fresh["processing_version"] != current["processing_version"]:
                        continue
                    unit.update(
                        "ingest_job_items",
                        item["id"],
                        status="error",
                        issue_codes=sorted(
                            (set(current["issue_codes"]) - {"reclassify_requested"})
                            | {"processing_error"}
                        ),
                    )
                    unit.event(
                        UUID(job_id),
                        "item.error",
                        f"Ошибка обработки: {type(error).__name__}; raw сохранён",
                        item_id=item["id"],
                        level="error",
                    )
        with self.store.transaction() as unit:
            counts = unit.refresh_counts(UUID(job_id))
            job = unit.get("ingest_jobs", job_id)
            status = (
                "completed_with_issues"
                if counts["needs_review_count"]
                or counts["error_count"]
                or counts["taxonomy_gap_count"]
                else "completed"
            )
            unit.update("ingest_jobs", job_id, status=status, finished_at=datetime.now(UTC))
            self.source_status(unit, job_id, "completed")
            unit.event(
                job["id"],
                "job.completed",
                "Обработка завершена",
                data=counts,
            )

    def stage(self, item: dict, status: str) -> None:
        """Фиксирует реальный текущий шаг, не держа SQL-транзакцию во время inference."""
        with self.store.transaction() as unit:
            labels = {
                "understanding": "VLM: определяю тип изделия",
                "matching": "Проверяю идентичность Product",
                "classifying": "Ищу подходящие категории",
            }
            norm = unit.get("normalized_records", item["normalized_record_id"])
            unit.update("ingest_job_items", item["id"], status=status)
            unit.update("ingest_jobs", item["job_id"], status="running")
            unit.event(
                item["job_id"],
                "item.stage",
                labels.get(status, status),
                item_id=item["id"],
                stage=status,
                data={"source_name": norm["normalized_name"]},
            )

    def process_item(self, item: dict) -> None:
        """Ограничивает бюджет VLM для всего конвейера одной позиции."""
        with self.inference.budget() as usage:
            self.process_with_budget(item, usage)

    def process_with_budget(self, item: dict, usage: dict) -> None:
        """Сохраняет независимость установления идентичности и классификации."""
        reclassify = "reclassify_requested" in item["issue_codes"]
        with self.store.transaction() as unit:
            norm = unit.get("normalized_records", item["normalized_record_id"])
            fields = norm["attributes"]["fields"]
            decisions = unit.all("classification_decisions", normalized_record_id=norm["id"])
            manual = [d for d in decisions if d["status"] == "approved" and d["method"] == "manual"]
            matches = unit.all("product_match_decisions", normalized_record_id=norm["id"])
            manual_matches = sorted(
                [m for m in matches if m["stage"] == "manual"], key=lambda m: m["created_at"]
            )
            rejected_ids = {m["product_id"] for m in manual_matches if m["status"] == "rejected"}
            if manual:
                decision = max(manual, key=lambda d: d["created_at"])
                manual_action = decision["evidence"].get("manual_action")
                if manual_action in {"not_product", "leave_uncategorized"}:
                    self.finish_item(
                        unit,
                        item,
                        "not_product" if manual_action == "not_product" else "needs_review",
                        None,
                        None if manual_action == "not_product" else decision["product_id"],
                    )
                    return
                linked_product = decision["product_id"]
                if manual_matches:
                    linked_product = (
                        manual_matches[-1]["product_id"]
                        if manual_matches[-1]["status"] == "confirmed"
                        else None
                    )
                unit.freeze_taxonomy()
                if self.keep_archived_product(unit, item, linked_product):
                    return
                category = unit.assignable_path(decision["category_id"])
                if category:
                    linked_product = self.ensure_assignment_product(unit, item, linked_product)
                    self.link_saved_assignment(unit, item, decision, linked_product)
                self.finish_item(
                    unit,
                    item,
                    "assigned" if category else "needs_review",
                    decision["category_id"] if category else None,
                    linked_product,
                )
                return
            if reclassify:
                product = unit.get("products", item["product_id"]) if item["product_id"] else None
            else:
                product = unit.confirmed_product(fields)
            if product and product["id"] in rejected_ids:
                product = None
            if manual_matches and manual_matches[-1]["status"] == "confirmed":
                product = unit.get("products", manual_matches[-1]["product_id"])
            if product and not reclassify:
                unit.freeze_taxonomy()
                unit.add(
                    "product_match_decisions",
                    normalized_record_id=norm["id"],
                    product_id=product["id"],
                    stage="fast_identifiers",
                    method="confirmed_issuer_mpn",
                    status="confirmed",
                    evidence={"issuer": fields.get("manufacturer") or fields.get("brand")},
                )
        product_id = product["id"] if product else None
        if self.assign_existing(
            item, norm, product_id, fields, {"product_type": fields["name"]}, allow_hint=False
        ):
            return
        self.stage(item, "understanding")
        try:
            answer = self.understand(item, norm, usage, reclassify)
        except InvalidModelOutput:
            answer = raw_type_understanding(fields)
            if answer is None:
                raise
            usage["understanding_origin"] = "raw_name"
        if reclassify:
            self.classify_saved(item, norm, product, answer, usage)
            return
        if (
            answer["status"] != "recognized"
            or answer["record_kind"] != "product_candidate"
            or not answer["product_type"]
            or is_bare_identifier(fields)
        ):
            status = (
                "not_product"
                if answer["record_kind"] in {"category_listing", "other"}
                else "needs_review"
            )
            with self.store.transaction() as unit:
                unit.add(
                    "product_match_decisions",
                    normalized_record_id=norm["id"],
                    stage="after_understanding",
                    method="insufficient_product_evidence",
                    status="unresolved",
                    evidence={"answer_1": answer},
                )
                self.finish_item(unit, item, status)
            return
        self.stage(item, "matching")
        profile = identity_profile(fields, answer)
        with self.store.transaction() as unit:
            unit.freeze_taxonomy()
            previous = [
                d
                for d in matches
                if d["product_id"]
                and d["status"] in {"candidate", "confirmed"}
                and d["product_id"] not in rejected_ids
            ]
            method = "existing_record"
            if not product and previous:
                saved = max(previous, key=lambda d: d["created_at"])
                product = unit.get("products", saved["product_id"])
            if not product:
                lookup = {
                    **fields,
                    **{key: claim_value(profile, key) for key in ("brand", "manufacturer", "mpn")},
                }
                product = unit.confirmed_product(lookup) or unit.find_identity_product(profile)
                if product and product["id"] in rejected_ids:
                    product = None
                method = "source_identity"
            if product and self.keep_archived_product(unit, item, product["id"]):
                return
            reused = product is not None
            if product is None:
                product = self.create_provisional(unit, item, fields, answer, profile)
            else:
                unit.remember_identity(product["id"], profile)
            unit.add(
                "product_match_decisions",
                normalized_record_id=norm["id"],
                product_id=product["id"],
                stage="after_understanding",
                method=method if reused else "new_provisional_no_identity_merge",
                status="confirmed" if reused else "candidate",
                evidence={"answer_1": answer},
            )
            unit.update("ingest_job_items", item["id"], product_id=product["id"])
            unit.attach_offer(item["raw_record_id"], product["id"])
        self.classify_saved(item, norm, product, answer, usage)

    def understand(self, item: dict, norm: dict, usage: dict, saved: bool = False) -> dict:
        """Повторно использует валидный answer_1 и хранит технические попытки отдельно."""
        fields = norm["attributes"]["fields"]
        bundle = evidence_bundle(
            str(norm["id"]),
            {"fields": fields},
            norm["attributes"]["locator"],
            self.settings.inference_max_input_chars,
        )
        values = {"evidence_bundle_json": bundle}
        provenance = self.inference.provenance(1, values, resolve=False)
        with self.store.transaction() as unit:
            if saved:
                previous = [
                    row
                    for row in unit.all("product_understandings", normalized_record_id=norm["id"])
                    if row["status"] in {"recognized", "insufficient_evidence"}
                    and row["answer_json"]
                    and row["model_revision"].startswith("mock") == (self.inference.mode == "mock")
                ]
                if previous:
                    return max(previous, key=lambda row: row["created_at"])["answer_json"]
        try:
            provenance = self.inference.provenance(1, values)
            with self.store.transaction() as unit:
                cached = unit.all(
                    "product_understandings", normalized_record_id=norm["id"], **provenance
                )
            if cached and cached[0]["status"] in {"recognized", "insufficient_evidence"}:
                return cached[0]["answer_json"]
            result = self.inference.answer(1, values)
        except (InvalidModelOutput, DependencyUnavailable) as error:
            usage["answer_1_issues"] = getattr(error, "attempts", [])
            with self.store.transaction() as unit:
                unit.unique(
                    "product_understandings",
                    {"normalized_record_id": norm["id"], **provenance},
                    status="invalid_output"
                    if isinstance(error, InvalidModelOutput)
                    else "unavailable",
                    raw_response_text=getattr(error, "raw_response", None),
                )
                if usage["answer_1_issues"]:
                    unit.add(
                        "classification_decisions",
                        normalized_record_id=norm["id"],
                        product_id=item["product_id"],
                        method="none",
                        selection_type="none",
                        status="needs_review",
                        evidence={
                            "answer_1_issues": usage["answer_1_issues"],
                            "mode": self.inference.mode,
                        },
                    )
            raise
        usage["answer_1_issues"] = result.get("attempts", [])
        with self.store.transaction() as unit:
            unit.save_understanding(norm["id"], provenance, result)
            if usage["answer_1_issues"]:
                unit.event(
                    item["job_id"],
                    "classification.repaired",
                    "Ответ о типе изделия исправлен",
                    item_id=item["id"],
                    data={"codes": [value["code"] for value in usage["answer_1_issues"]]},
                )
                unit.add(
                    "classification_decisions",
                    normalized_record_id=norm["id"],
                    product_id=item["product_id"],
                    method="none",
                    selection_type="none",
                    status="superseded",
                    evidence={
                        "answer_1_issues": usage["answer_1_issues"],
                        "mode": self.inference.mode,
                    },
                )
        return result["answer_json"]

    def classify_saved(
        self, item: dict, norm: dict, product: dict | None, answer: dict, usage: dict
    ) -> None:
        """Выбирает категорию по утверждённым данным или VLM, независимо от Product."""
        product_id = product["id"] if product else None
        fields = norm["attributes"]["fields"]
        if (
            answer["status"] != "recognized"
            or answer["record_kind"] != "product_candidate"
            or not answer["product_type"]
            or is_bare_identifier(fields)
        ):
            with self.store.transaction() as unit:
                status = (
                    "not_product"
                    if answer["record_kind"] in {"category_listing", "other"}
                    else "needs_review"
                )
                self.finish_item(unit, item, status, product_id=product_id)
            return
        if self.assign_existing(item, norm, product_id, fields, answer):
            return
        self.stage(item, "classifying")
        with self.store.transaction() as unit:
            unit.event(
                item["job_id"],
                "classification.type",
                "Определён тип: " + answer["product_type"][:200],
                item_id=item["id"],
                data={"origin": usage.get("understanding_origin", "answer_1")},
            )

        def emit(kind: str, message: str, data: dict) -> None:
            """Фиксирует переходы без сырого текста модели в SSE."""
            with self.store.transaction() as unit:
                unit.event(item["job_id"], kind, message, item_id=item["id"], data=data)

        result = self.classifier.select(
            fields, {key: value for key, value in answer.items() if key != "identifiers"}, emit
        )
        with self.store.transaction() as unit:
            unit.freeze_taxonomy()
            fresh = unit.get("ingest_job_items", item["id"], lock=True)
            if fresh["processing_version"] != item["processing_version"]:
                return
            existing = unit.get("products", product_id, lock=True) if product_id else None
            if existing and existing["status"] == "retired":
                self.finish_item(unit, item, "needs_review", product_id=product_id)
                return
            if (
                existing
                and existing["category_id"]
                and unit.assignable_path(existing["category_id"])
            ):
                self.save_assignment(
                    unit, item, existing["category_id"], product_id, "existing_product"
                )
                return
            if existing and existing["category_id"] and unit.has_manual_product_category(existing):
                self.finish_item(unit, item, "needs_review", product_id=product_id)
                return
            chosen = result.category
            row = unit.assignable_path(chosen["category_id"]) if chosen else None
            if chosen and (
                not row
                or any(row[k] != chosen[k] for k in ("path_ids", "path_names", "review_status"))
            ):
                result.reason = "Категория изменилась после выбора VLM"
                result.unresolved = True
                row = None
            if row and approved_category_exclusion(
                fields["name"],
                {
                    "exclusions": row["semantic_exclusions"]
                    if row["review_status"] == "approved"
                    else None,
                    "ancestor_exclusions": row["ancestor_exclusions"],
                },
            ):
                result.reason = "При записи обнаружено утверждённое исключение категории"
                result.unresolved = True
                row = None
            evidence = {
                **result.evidence,
                **usage,
                "answer_1": answer,
                "mode": self.inference.mode,
                "strategy": result.strategy,
                "reason": result.reason,
                "model_logical_id": result.provenance.get("model_logical_id"),
            }
            provenance = {
                "prompt_sha256": result.provenance.get("prompt_sha256"),
                "model_revision": result.provenance.get("model_revision"),
                "embedding_model_revision": self.inference.embedding_revision,
                "taxonomy_revision": unit.taxonomy_revision(),
                "candidates": result.candidates,
            }
            if row:
                self.save_assignment(
                    unit,
                    item,
                    row["id"],
                    product_id,
                    "retrieval_vlm",
                    result.selection_type,
                    evidence,
                    provenance,
                )
            else:
                self.save_unresolved(unit, item, product_id, answer, result, evidence, provenance)

    def assign_existing(self, item, norm, product_id, fields, answer, *, allow_hint=True) -> bool:
        """Принимает действующую связь товара или утверждённое соответствие из SQL."""
        with self.store.transaction() as unit:
            unit.freeze_taxonomy()
            product = unit.get("products", product_id, lock=True) if product_id else None
            if product and product["status"] == "retired":
                self.finish_item(unit, item, "needs_review", product_id=product_id)
                return True
            decisions = unit.all("classification_decisions", normalized_record_id=norm["id"])
            for decision in sorted(decisions, key=lambda row: row["created_at"], reverse=True):
                if (
                    decision["status"] == "approved"
                    and decision["category_id"]
                    and (product_id is None or decision["product_id"] in {None, product_id})
                    and (not product or product["category_id"] == decision["category_id"])
                ):
                    if self.keep_archived_product(unit, item, decision["product_id"]):
                        return True
                    if not unit.assignable_path(decision["category_id"]):
                        continue
                    product_id = self.ensure_assignment_product(
                        unit, item, product_id, decision["evidence"].get("answer_1")
                    )
                    linked = self.link_saved_assignment(unit, item, decision, product_id)
                    self.finish_item(
                        unit,
                        item,
                        "assigned" if linked else "needs_review",
                        decision["category_id"] if linked else None,
                        product_id,
                    )
                    return True
            if product and product["category_id"] and unit.assignable_path(product["category_id"]):
                self.save_assignment(
                    unit, item, product["category_id"], product_id, "existing_product"
                )
                return True
            if product and product["category_id"] and unit.has_manual_product_category(product):
                self.finish_item(unit, item, "needs_review", product_id=product_id)
                unit.event(
                    item["job_id"],
                    "classification.unresolved",
                    "Категория ручного решения недоступна",
                    item_id=item["id"],
                    level="warning",
                )
                return True
            if (
                "reclassify_requested" in item["issue_codes"]
                and item["product_id"] is None
                and item["category_id"]
                and unit.assignable_path(item["category_id"])
            ):
                self.save_assignment(
                    unit, item, item["category_id"], product_id, "existing_product"
                )
                return True
            if not allow_hint:
                return False
            hint = unit.approved_category_hint(item["raw_record_id"], fields, answer)
            explicit_type = (
                answer
                if answer.get("record_kind") == "product_candidate"
                else raw_type_understanding(fields)
            )
            if hint and (product_id or explicit_type) and unit.assignable_path(hint[0]):
                self.save_assignment(
                    unit,
                    item,
                    hint[0],
                    product_id,
                    hint[1],
                    evidence={"approved_sql_hint": True, "answer_1": explicit_type},
                )
                return True
        return False

    def keep_archived_product(self, unit, item: dict, product_id) -> bool:
        """Сохраняет связь с удалённым из каталога товаром без повторного создания."""
        if product_id and unit.get("products", product_id, lock=True)["status"] == "retired":
            self.finish_item(unit, item, "needs_review", product_id=product_id)
            return True
        return False

    def create_provisional(
        self,
        unit,
        item: dict,
        fields: dict,
        answer: dict | None = None,
        profile: dict | None = None,
    ) -> dict:
        """Создаёт самостоятельный Product и сохраняет задание создания для отчёта."""
        if not fields.get("name"):
            raise CatalogError("Для создания Product нужно исходное наименование изделия")
        product = unit.add(
            "products",
            canonical_name=fields["name"],
            product_type=answer.get("product_type") if answer else None,
            unit_code=fields.get("unit"),
            attributes={
                "identity_fields": {k: fields.get(k) for k in ("unit", "quantity")},
                "understanding": answer,
                "created_in_job_id": str(item["job_id"]),
            },
        )
        unit.remember_identity(product["id"], profile or identity_profile(fields, answer))
        return product

    def ensure_assignment_product(self, unit, item, product_id, answer=None):
        """Переиспользует связь той же нормализованной записи или создаёт свой Product."""
        if product_id:
            return product_id
        normalized_id = item["normalized_record_id"]
        matches = unit.all("product_match_decisions", normalized_record_id=normalized_id)
        newest = sorted(matches, key=lambda match: match["created_at"], reverse=True)
        manual = {}
        for match in newest:
            if match["stage"] == "manual":
                manual.setdefault(match["product_id"], match["status"])
        for match in newest:
            candidate = match["product_id"]
            if (
                candidate
                and manual.get(candidate) != "rejected"
                and match["status"] in {"candidate", "confirmed"}
                and unit.get("products", candidate)["status"] != "retired"
            ):
                return candidate
        norm = unit.get("normalized_records", normalized_id)
        product = self.create_provisional(unit, item, norm["attributes"]["fields"], answer)
        unit.add(
            "product_match_decisions",
            normalized_record_id=normalized_id,
            product_id=product["id"],
            stage="after_understanding",
            method="new_provisional_no_identity_merge",
            status="candidate",
            evidence={"origin": "category_assignment"},
        )
        return product["id"]

    def link_saved_assignment(self, unit, item, decision, product_id) -> bool:
        """Связывает проверенное назначение с Product, сохраняя автора и исходный UUID."""
        category_id = decision["category_id"]
        product = unit.get("products", product_id, lock=True)
        if product["category_id"] != category_id:
            if decision["method"] != "manual" and unit.has_manual_product_category(product):
                return False
            unit.update("products", product_id, category_id=category_id)
        if decision["product_id"] != product_id:
            unit.update("classification_decisions", decision["id"], product_id=product_id)
        unit.link_assigned_items(item["normalized_record_id"], category_id, product_id)
        return True

    def save_assignment(
        self,
        unit,
        item,
        category_id,
        product_id,
        method,
        selection_type="exact",
        evidence=None,
        provenance=None,
    ) -> None:
        """Атомарно назначает категорию, завершает старые предложения и открытые задачи."""
        normalized_id = item["normalized_record_id"]
        product_id = self.ensure_assignment_product(
            unit, item, product_id, (evidence or {}).get("answer_1")
        )
        product = unit.get("products", product_id, lock=True)
        if product["category_id"] and unit.has_manual_product_category(product):
            if not unit.assignable_path(product["category_id"]):
                self.finish_item(unit, item, "needs_review", product_id=product_id)
                return
            if product["category_id"] != category_id:
                category_id, method, selection_type = (
                    product["category_id"],
                    "existing_product",
                    "exact",
                )
                evidence, provenance = {}, {}
        for previous in unit.all("classification_decisions", normalized_record_id=normalized_id):
            if previous["status"] in {"approved", "proposed"} and previous["method"] != "manual":
                unit.update("classification_decisions", previous["id"], status="superseded")
        unit.add(
            "classification_decisions",
            normalized_record_id=normalized_id,
            product_id=product_id,
            category_id=category_id,
            method=method,
            selection_type=selection_type,
            status="approved",
            decided_by="system",
            evidence=evidence or {},
            **(provenance or {}),
        )
        unit.update("products", product_id, category_id=category_id)
        unit.link_assigned_items(normalized_id, category_id, product_id)
        for task in unit.all("review_tasks", normalized_record_id=normalized_id, status="open"):
            if task["kind"] in {"category_selection", "taxonomy_gap"}:
                unit.update(
                    "review_tasks",
                    task["id"],
                    status="resolved",
                    actor="system",
                    resolution={"category_id": str(category_id)},
                    resolved_at=datetime.now(UTC),
                )
        for gap in unit.all("taxonomy_gaps", normalized_record_id=normalized_id, status="open"):
            unit.update(
                "taxonomy_gaps",
                gap["id"],
                status="resolved",
                resolved_category_id=category_id,
                resolved_by="system",
                resolved_at=datetime.now(UTC),
            )
        self.finish_item(unit, item, "assigned", category_id, product_id)
        row = unit.assignable_path(category_id)
        unit.event(
            item["job_id"],
            "item.auto_assigned",
            "Назначено автоматически: " + " → ".join(row["path_names"]),
            item_id=item["id"],
            data={"path": row["path_names"], "method": method},
        )

    def save_unresolved(self, unit, item, product_id, answer, result, evidence, provenance) -> None:
        """Сохраняет причину и переиспользует открытую задачу при повторе."""
        normalized_id = item["normalized_record_id"]
        status = "needs_review" if result.unresolved else "no_suitable_category"
        unit.add(
            "classification_decisions",
            normalized_record_id=normalized_id,
            product_id=product_id,
            method="retrieval_vlm",
            selection_type="none",
            status=status,
            evidence=evidence,
            **provenance,
        )
        kind = "category_selection" if result.unresolved else "taxonomy_gap"
        if kind == "taxonomy_gap" and not unit.all(
            "taxonomy_gaps", normalized_record_id=normalized_id, status="open"
        ):
            unit.add(
                "taxonomy_gaps",
                normalized_record_id=normalized_id,
                product_id=product_id,
                proposed_type=answer["product_type"],
                evidence=evidence,
            )
        if not unit.all(
            "review_tasks", normalized_record_id=normalized_id, kind=kind, status="open"
        ):
            unit.add(
                "review_tasks",
                normalized_record_id=normalized_id,
                product_id=product_id,
                kind=kind,
                context={"reason": result.reason},
            )
        if result.technical_failure:
            item = {**item, "issue_codes": [*item["issue_codes"], "classification_technical"]}
        self.finish_item(
            unit,
            item,
            "needs_review" if result.unresolved else "taxonomy_gap",
            product_id=product_id,
        )
        unit.event(
            item["job_id"],
            "classification.unresolved",
            result.reason[:500],
            item_id=item["id"],
            level="warning",
        )

    def finish_item(self, unit, item: dict, status: str, category_id=None, product_id=None) -> None:
        """Коммитит результат позиции и event вместе со счётчиками."""
        if status == "assigned" and (product_id is None or category_id is None):
            raise CatalogError("Назначение требует Product и категорию")
        issue_codes = set(item["issue_codes"]) - {"reclassify_requested"}
        if status == "assigned":
            issue_codes -= {
                "classification_technical",
                "inference_unavailable",
                "invalid_output",
                "processing_error",
                "taxonomy_changed",
            }
        unit.update(
            "ingest_job_items",
            item["id"],
            status=status,
            category_id=category_id,
            product_id=product_id,
            issue_codes=sorted(issue_codes),
        )
        if product_id:
            unit.attach_offer(item["raw_record_id"], product_id)
        unit.event(
            item["job_id"],
            "item." + status,
            "Позиция: " + status,
            item_id=item["id"],
            data={"category_id": str(category_id) if category_id else None},
        )

    def valid_category(self, category_id, candidates=None) -> bool:
        """Повторно проверяет active/assignable/path/status по SQL перед назначением."""
        row = next((r for r in self.store.categories() if str(r["id"]) == str(category_id)), None)
        if (
            not row
            or not row["is_active"]
            or not row["ancestors_active"]
            or not row["is_assignable"]
            or row["review_status"] == "deprecated"
        ):
            return False
        previous = next((r for r in candidates or [] if r["category_id"] == str(category_id)), None)
        return (
            previous is None
            if candidates is None
            else bool(
                previous
                and previous["path_names"] == row["path_names"]
                and previous["review_status"] == row["review_status"]
            )
        )

    def reclassify(self, job_id, apply: bool = False, *, only_remaining: bool = False) -> dict:
        """Ставит прежние нерешённые позиции в очередь без чтения файлов и matching."""
        with self.store.transaction() as unit:
            job = unit.get("ingest_jobs", job_id, lock=apply)
            if job["status"] not in {"completed", "completed_with_issues"}:
                raise ConflictError("Повторная классификация доступна после завершения задания")
            items = unit.reclassification_items(job["id"], only_remaining)
            if apply:
                items = [item for item in items if unit.queue_reclassification(item)]
            if apply and items:
                unit.update(
                    "ingest_jobs",
                    job["id"],
                    status="queued",
                    cancel_requested=False,
                    finished_at=None,
                )
                unit.event(
                    job["id"],
                    "job.reclassify",
                    "Запрошена повторная классификация",
                    data={"count": len(items)},
                )
        if apply and items:
            self.notify_worker(job_id)
        return {"id": str(job_id), "eligible": len(items), "queued": bool(apply and items)}

    def archive_product(self, product_id, actor: str, key: str) -> dict:
        """Убирает Product из каталога, оставляя связи со строками и решениями."""
        request = {"operation": "archive_product", "product_id": str(product_id)}
        with self.store.transaction() as unit:
            cached = unit.command(actor, key, request)
            if cached:
                return cached
            unit.freeze_taxonomy()
            product = unit.get("products", product_id, lock=True)
            result = {
                "id": str(product["id"]),
                "status": "retired",
                **unit.retire_product(product_id),
            }
            unit.save_command(actor, key, request, result)
        return result

    def archive_not_products(self, job_id, expected_count: int, actor: str, key: str) -> dict:
        """Атомарно скрывает подтверждённое число строк «Не товар» одного задания."""
        request = {
            "operation": "archive_not_products",
            "job_id": str(job_id),
            "expected_count": expected_count,
        }
        with self.store.transaction() as unit:
            cached = unit.command(actor, key, request)
            if cached:
                return cached
            job = unit.get("ingest_jobs", job_id, lock=True)
            if job["status"] not in {"completed", "completed_with_issues"}:
                raise ConflictError("Удаление доступно после завершения задания")
            rows = unit.unarchived_not_products(job_id, lock=True)
            if not rows or len(rows) != expected_count:
                raise ConflictError("Список «Не товар» изменился. Обновите задание.")
            for item in rows:
                unit.update(
                    "ingest_job_items",
                    item["id"],
                    issue_codes=[*item["issue_codes"], "archived_not_product"],
                    processing_version=item["processing_version"] + 1,
                )
            result = {"id": str(job_id), "archived_count": len(rows)}
            unit.event(
                job_id, "job.not_products_archived", "Позиции «Не товар» скрыты", data=result
            )
            unit.save_command(actor, key, request, result)
        return result

    def control(self, job_id, action: str) -> dict:
        """Cancel сохраняет checkpoints; resume явно повторяет только технические ошибки."""
        with self.store.transaction() as unit:
            job = unit.get("ingest_jobs", job_id, lock=True)
            if action == "cancel":
                if job["status"] in TERMINAL_JOBS:
                    return json_value(job)
                status = "cancel_requested" if job["status"] == "running" else "canceled"
                if status == "canceled":
                    self.source_status(unit, job_id, "cancelled")
                unit.update("ingest_jobs", job_id, status=status, cancel_requested=True)
            elif action == "resume":
                if job["status"] not in TERMINAL_JOBS:
                    raise ConflictError("Возобновить можно остановленное или завершённое задание")
                items = unit.all("ingest_job_items", job_id=job["id"])
                self.source_status(unit, job_id, "pending")
                for item in items:
                    if item["status"] == "error" or set(item["issue_codes"]) & {
                        "inference_unavailable",
                        "invalid_output",
                        "processing_error",
                        "taxonomy_changed",
                    }:
                        unit.update(
                            "ingest_job_items",
                            item["id"],
                            status="pending",
                            issue_codes=[
                                c
                                for c in item["issue_codes"]
                                if c
                                not in {
                                    "inference_unavailable",
                                    "invalid_output",
                                    "processing_error",
                                    "taxonomy_changed",
                                }
                            ],
                        )
                unit.update(
                    "ingest_jobs",
                    job_id,
                    status="queued" if items else "preview_ready",
                    cancel_requested=False,
                    finished_at=None,
                )
            else:
                raise CatalogError("Неизвестная команда задания")
            unit.event(job["id"], "job." + action, "Запрошено: " + action)
        if action == "resume":
            self.notify_worker(job_id)
        return self.job(job_id)

    def job(self, job_id) -> dict:
        """Возвращает status и реальные durable counters."""
        with self.store.transaction() as unit:
            return json_value({**self.job_summary(unit, job_id), "mode": self.inference.mode})

    def job_summary(self, unit, job_id) -> dict:
        """Помечает старый импорт с незаполненным каталогом как завершённый с замечаниями."""
        job = {**unit.get("ingest_jobs", job_id), **unit.assignment_counts(job_id)}
        if job["status"] == "completed" and job["assigned_without_product_count"]:
            job["status"] = "completed_with_issues"
        return job

    def items(self, job_id, status: str | None, query: str, offset: int, limit: int) -> dict:
        """Делегирует SQL pagination без загрузки raw payload всех позиций в браузер."""
        with self.store.transaction() as unit:
            unit.get("ingest_jobs", job_id)
            return json_value(unit.items_page(job_id, status, query, offset, limit))

    def item_detail(self, item_id, job_id=None) -> dict:
        """Показывает объяснение по последнему решению и пригодные для назначения варианты."""
        with self.store.transaction() as unit:
            detail = unit.item_details(item_id)
            item = detail["item"]
            if job_id is not None and item["job_id"] != job_id:
                raise CatalogError("Позиция не принадлежит заданию")
            latest = max(
                (d for d in detail["decisions"] if d["status"] != "superseded"),
                key=lambda d: d["created_at"],
                default=None,
            )
            evidence = latest["evidence"] if latest else {}
            detail["classification_reason"] = evidence.get("reason")
            detail["suggested_categories"] = []
            if (
                latest
                and item["status"] == "needs_review"
                and latest["status"] in {"needs_review", "proposed"}
                and (not detail["product"] or detail["product"]["status"] != "retired")
            ):
                name = detail["normalized"]["attributes"]["fields"]["name"]
                for category_id in suggested_category_ids(latest):
                    row = unit.assignable_path(category_id)
                    if row:
                        candidate = candidate_view(row)
                        if not approved_category_exclusion(name, candidate):
                            detail["suggested_categories"].append(candidate)
                    if len(detail["suggested_categories"]) == 5:
                        break
            return json_value(detail)

    def report(self, job_id) -> dict:
        """Отчёт отражает фактические counters, issues и provenance без секретов."""
        with self.store.transaction() as unit:
            job = self.job_summary(unit, job_id)
            rows = unit.items_page(
                job_id, None, "", 0, self.settings.upload_max_rows, include_archived=True
            )["items"]
            files = unit.all("ingest_job_files", job_id=job["id"])
            norm_ids = list({r["normalized_record_id"] for r in rows if r["normalized_record_id"]})
            normalized = {n["id"]: n for n in unit.related("normalized_records", "id", norm_ids)}
            decisions = unit.related(
                "classification_decisions",
                "normalized_record_id",
                norm_ids,
                "normalized_record_id",
                "created_at",
                "method",
                "status",
            )
            tasks = unit.related(
                "review_tasks", "normalized_record_id", norm_ids, "normalized_record_id", "id"
            )
            database_counts = unit.catalog_counts()
            duplicate_key_groups = unit.duplicate_key_groups()
        categories = {str(c["id"]): c for c in self.store.categories()}
        files_by_id = {f["id"]: f for f in files}
        latest = {}
        for decision in decisions:
            key = decision["normalized_record_id"]
            if key not in latest or latest[key]["created_at"] < decision["created_at"]:
                latest[key] = decision
        task_ids = defaultdict(list)
        for task in tasks:
            task_ids[task["normalized_record_id"]].append(task["id"])
        for row in rows:
            key = row["normalized_record_id"]
            norm = normalized.get(key, {})
            last = latest.get(key)
            row.update(
                source_file=files_by_id[row["file_id"]]["original_filename"],
                sheet=row["sheet_name"],
                row=row["row_number"],
                raw_name=norm.get("attributes", {}).get("raw_name", row["name"]),
                category_path=categories.get(str(row["category_id"]), {}).get("path_names"),
                decision_method=last["method"] if last else None,
                decision_status=last["status"] if last else None,
                review_task_ids=task_ids[key],
            )
        return json_value(
            {
                "job": job,
                "mode": self.inference.mode,
                "files": [
                    {
                        "name": f["original_filename"],
                        "sha256": f["content_sha256"],
                        "profile": f["profile"],
                    }
                    for f in files
                ],
                "items": rows,
                "statuses": dict(Counter(row["status"] for row in rows)),
                "issue_counts": dict(Counter(code for row in rows for code in row["issue_codes"])),
                "database_counts": database_counts,
                "duplicate_key_groups": duplicate_key_groups,
                "taxonomy": {
                    "records": len(categories),
                    "roots": sum(c["parent_id"] is None for c in categories.values()),
                    "max_depth": max((len(c["path_ids"]) for c in categories.values()), default=0),
                    "levels": dict(
                        sorted(Counter(len(c["path_ids"]) for c in categories.values()).items())
                    ),
                    "active_projection": sum(
                        c["is_active"] and c["ancestors_active"] for c in categories.values()
                    ),
                },
                "index": self.index.status(),
                "price_policy": "Исторические наблюдения; current price не формируется",
            }
        )

    def request_reindex(self) -> None:
        """Ставит переиндексацию в существующий транзакционный outbox."""
        with self.store.transaction() as unit:
            unit.request_reindex()

    def cleanup_uploads(self) -> int:
        """Удаляет только просроченные UUID-файлы завершённых jobs; SQL raw/audit сохраняются."""
        with self.store.transaction() as unit:
            files = unit.expired_uploads(self.settings.upload_ttl_seconds)
        deleted = 0
        root = self.settings.upload_dir.resolve()
        for file in files:
            path = Path(file["storage_key"]).resolve()
            if (
                path.is_relative_to(root)
                and path.stem == str(file["id"])
                and path.parent.name == str(file["job_id"])
                and path.is_file()
            ):
                path.unlink()
                deleted += 1
        return deleted

    def create_category(self, values: dict, actor: str, key: str) -> dict:
        """Создаёт custom UUID после проверки родителя и точного sibling-дубликата."""
        name = normalize_text(values.get("name"))
        reason = normalize_text(values.get("reason"))
        if not name or len(name) > 512 or not reason:
            raise CatalogError("Укажите название и основание новой категории")
        parent = UUID(values["parent_id"]) if values.get("parent_id") else None
        request = {"operation": "create_category", **values}
        with self.store.transaction() as unit:
            cached = unit.command(actor, key, request)
            if cached:
                return cached
            unit.command("category_siblings", str(parent), {})
            if parent:
                row = unit.get("categories", parent, lock=True)
                if not row["is_active"] or row["review_status"] == "deprecated":
                    raise CatalogError("Родитель недоступен")
                parent_path = next(r for r in self.store.categories() if r["id"] == parent)
                if (
                    not parent_path["ancestors_active"]
                    or len(parent_path["path_ids"]) >= self.settings.taxonomy_max_depth
                ):
                    raise CatalogError("Родительская ветвь неактивна или превышен лимит глубины")
            siblings = unit.all("categories", parent_id=parent)
            if any(normalize_text(c["name"]).casefold() == name.casefold() for c in siblings):
                raise ConflictError("Категория с таким названием уже есть у выбранного родителя")
            category_id = uuid4()
            category = unit.add(
                "categories",
                id=category_id,
                name=name,
                parent_id=parent,
                slug="custom-" + category_id.hex,
                sort_order=len(siblings),
                source="custom",
                is_assignable=bool(values.get("is_assignable", True)),
                review_status="approved",
                semantic_description=values.get("description"),
                semantic_purpose=values.get("purpose"),
                semantic_exclusions=values.get("exclusions"),
            )
            unit.add(
                "category_change_sets",
                action="keep",
                category_id=category_id,
                reason=reason,
                status="applied",
                approved_by=actor,
                applied_at=datetime.now(UTC),
                preview={"operation": "create", "category": json_value(category)},
            )
            response = {"id": str(category_id), "name": name, "sync_status": "sync_pending"}
            unit.save_command(actor, key, request, response)
        return response

    def decide(self, item_id, values: dict, actor: str, key: str) -> dict:
        """Ручное назначение имеет приоритет; identity matching остаётся отдельной командой."""
        request = {"operation": "review", "item_id": str(item_id), **values}
        action = values.get("action", "assign_category")
        reason = normalize_text(values.get("reason")) or ""
        if not reason and action != "assign_category":
            raise CatalogError("Укажите основание решения")
        with self.store.transaction() as unit:
            cached = unit.command(actor, key, request)
            if cached:
                return cached
            item = unit.get("ingest_job_items", item_id)
            unit.lock_record(item["normalized_record_id"])
            item = unit.get("ingest_job_items", item_id, lock=True)
            if (
                item["processing_version"] != values.get("expected_version")
                or "archived_not_product" in item["issue_codes"]
            ):
                raise ConflictError("Позиция изменилась; обновите данные")
            category_id = UUID(values["category_id"]) if values.get("category_id") else None
            if action == "assign_category":
                unit.freeze_taxonomy()
                if not unit.assignable_path(category_id):
                    raise CatalogError("Категория недоступна для назначения")
                if (
                    item["product_id"]
                    and unit.get("products", item["product_id"], lock=True)["status"] == "retired"
                ):
                    raise ConflictError("Товар удалён из каталога")
                product_id = self.ensure_assignment_product(unit, item, item["product_id"])
                for decision in unit.all(
                    "classification_decisions", normalized_record_id=item["normalized_record_id"]
                ):
                    if decision["status"] in {"approved", "proposed"}:
                        unit.update("classification_decisions", decision["id"], status="superseded")
                unit.add(
                    "classification_decisions",
                    normalized_record_id=item["normalized_record_id"],
                    product_id=product_id,
                    category_id=category_id,
                    method="manual",
                    selection_type="exact",
                    status="approved",
                    evidence={"reason": reason},
                    decided_by=actor,
                )
                unit.assign_record_items(item["normalized_record_id"], category_id, product_id)
                unit.update("products", product_id, category_id=category_id)
                unit.attach_offer(item["raw_record_id"], product_id)
                for gap in unit.all(
                    "taxonomy_gaps",
                    normalized_record_id=item["normalized_record_id"],
                    status="open",
                ):
                    unit.update(
                        "taxonomy_gaps",
                        gap["id"],
                        status="resolved",
                        resolved_category_id=category_id,
                        resolved_by=actor,
                        resolved_at=datetime.now(UTC),
                    )
            elif action in {"not_product", "leave_uncategorized"}:
                for previous in unit.all(
                    "classification_decisions", normalized_record_id=item["normalized_record_id"]
                ):
                    if previous["status"] in {"approved", "proposed"}:
                        unit.update("classification_decisions", previous["id"], status="superseded")
                unit.add(
                    "classification_decisions",
                    normalized_record_id=item["normalized_record_id"],
                    product_id=item["product_id"],
                    method="manual",
                    selection_type="none",
                    status="approved",
                    decided_by=actor,
                    evidence={"manual_action": action, "reason": reason},
                )
                unit.update(
                    "ingest_job_items",
                    item_id,
                    status="not_product" if action == "not_product" else "needs_review",
                    category_id=None,
                    product_id=None if action == "not_product" else item["product_id"],
                    processing_version=item["processing_version"] + 1,
                )
            elif action in {"confirm_product", "reject_product"}:
                product_id = (
                    UUID(values["product_id"]) if values.get("product_id") else item["product_id"]
                )
                if product_id is None:
                    raise CatalogError("Выберите Product отдельно от Category")
                if unit.get("products", product_id, lock=True)["status"] == "retired":
                    raise ConflictError("Товар удалён из каталога")
                unit.add(
                    "product_match_decisions",
                    normalized_record_id=item["normalized_record_id"],
                    product_id=product_id,
                    stage="manual",
                    method=action,
                    status="confirmed" if action == "confirm_product" else "rejected",
                    evidence={"reason": values["reason"]},
                    decided_by=actor,
                )
                if action == "confirm_product":
                    unit.update("products", product_id, status="identified")
                    unit.update(
                        "ingest_job_items",
                        item_id,
                        product_id=product_id,
                        processing_version=item["processing_version"] + 1,
                    )
                    unit.attach_offer(item["raw_record_id"], product_id)
                    norm = unit.get("normalized_records", item["normalized_record_id"])
                    fields = norm["attributes"]["fields"]
                    issuer = fields.get("manufacturer") or fields.get("brand")
                    if issuer and fields.get("mpn"):
                        identifier = unit.unique(
                            "product_identifiers",
                            {
                                "product_id": product_id,
                                "identifier_type": "MPN",
                                "issuer": normalized_key(issuer),
                                "normalized_value": normalized_key(fields["mpn"]),
                            },
                            raw_value=fields["mpn"],
                            status="confirmed",
                            evidence={"actor": actor, "reason": values["reason"]},
                        )
                        unit.update(
                            "product_identifiers",
                            identifier["id"],
                            status="confirmed",
                            evidence={"actor": actor, "reason": values["reason"]},
                        )
                elif (
                    item["product_id"] == product_id
                    and item["status"] == "assigned"
                    and item["category_id"]
                ):
                    unit.freeze_taxonomy()
                    replacement = self.ensure_assignment_product(unit, item, None)
                    unit.update("products", replacement, category_id=item["category_id"])
                    unit.link_assigned_items(
                        item["normalized_record_id"], item["category_id"], replacement, product_id
                    )
                    for decision in unit.all(
                        "classification_decisions",
                        normalized_record_id=item["normalized_record_id"],
                        category_id=item["category_id"],
                        status="approved",
                    ):
                        unit.update(
                            "classification_decisions", decision["id"], product_id=replacement
                        )
                else:
                    unit.update(
                        "ingest_job_items",
                        item_id,
                        product_id=None if item["product_id"] == product_id else item["product_id"],
                        processing_version=item["processing_version"] + 1,
                    )
            else:
                raise CatalogError("Неизвестное ручное решение")
            for task in unit.all(
                "review_tasks", normalized_record_id=item["normalized_record_id"], status="open"
            ):
                if (
                    action == "assign_category"
                    and task["kind"] in {"category_selection", "taxonomy_gap"}
                ) or (action != "assign_category" and task["kind"] == "product_match"):
                    unit.update(
                        "review_tasks",
                        task["id"],
                        status="resolved",
                        actor=actor,
                        resolution=values,
                        resolved_at=datetime.now(UTC),
                    )
            unit.event(
                item["job_id"],
                "review.decided",
                "Ручное решение сохранено",
                item_id=item["id"],
                data={"action": action, "actor": actor},
            )
            response = json_value(unit.get("ingest_job_items", item_id))
            unit.save_command(actor, key, request, response)
        return response

    def bulk_preview(self, values: dict) -> dict:
        """Показывает строго выбранные UUID/версии и категорию до пакетного применения."""
        rows = values.get("items", [])
        if not rows or len(rows) > 200 or len({r["id"] for r in rows}) != len(rows):
            raise CatalogError("Выберите от 1 до 200 разных позиций")
        if not self.valid_category(values.get("category_id")):
            raise CatalogError("Укажите доступную категорию для пакетного назначения")
        preview = []
        with self.store.transaction() as unit:
            for selected in rows:
                item = unit.get("ingest_job_items", selected["id"])
                if item["processing_version"] != selected["expected_version"]:
                    raise ConflictError("Одна из выбранных позиций изменилась")
                norm = unit.get("normalized_records", item["normalized_record_id"])
                preview.append({**selected, "name": norm["normalized_name"]})
        return {
            "items": preview,
            "category_id": values["category_id"],
            "reason": values.get("reason", ""),
            "preview_sha256": fingerprint(values),
        }

    def bulk_apply(self, values: dict, actor: str, key: str) -> dict:
        """Каждое назначение атомарно; конфликты возвращаются явно, без скрытого overwrite."""
        request = {"items": values["items"], "category_id": values["category_id"]}
        if "reason" in values:
            request["reason"] = values["reason"]
        if fingerprint(request) != values.get("preview_sha256"):
            raise CatalogError("Пакет отличается от подтверждённого preview")
        if not request["items"] or len(request["items"]) > 200:
            raise CatalogError("Неверный размер пакета")
        with self.store.transaction() as unit:
            cached = unit.command(actor, key, values)
            if cached:
                return cached
            result = {"applied": [], "conflicts": []}
            for selected in request["items"]:
                try:
                    self.decide(
                        selected["id"],
                        {
                            "action": "assign_category",
                            "category_id": request["category_id"],
                            "reason": request.get("reason", ""),
                            "expected_version": selected["expected_version"],
                        },
                        actor,
                        key + ":" + selected["id"],
                    )
                    result["applied"].append(selected["id"])
                except ConflictError:
                    result["conflicts"].append(selected["id"])
            unit.save_command(actor, key, values, result)
        return result
