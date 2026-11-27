# app/catalog_cli.py
"""CLI этапа 02 обращается к тем же use-case, что Swagger и браузер."""

import json
import re

from app.application.product_merge import ProductMerge
from app.catalog_bootstrap import build_catalog
from app.domain.catalog import CatalogError


def dispatch_catalog(args, settings) -> dict:
    """Не импортирует данные без --apply; preview CLI не пишет даже job/raw в БД."""
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "app.presentation.api:create_app", factory=True, host="0.0.0.0", port=settings.api_port
        )
        return {"status": "stopped"}
    if args.command == "worker":
        from app.workers.catalog import run

        return run(settings, once=args.once)
    if args.command == "import-file" and not args.apply:
        from app.infrastructure.catalog_formats import CatalogFileParser

        if not args.input:
            raise CatalogError("Для preview укажите --input")
        options = (
            json.loads(args.mapping_file.read_text(encoding="utf-8")) if args.mapping_file else {}
        )
        parsed = CatalogFileParser(settings).parse(
            args.input, args.input.name, {"profile": args.profile, **options}
        )
        return {
            "dry_run": True,
            "profile": parsed.profile,
            "rows": len(parsed.rows),
            "requires_mapping": parsed.requires_mapping,
            "mapping": parsed.mapping,
            "warnings": parsed.warnings,
        }
    service = build_catalog(settings)
    if args.command == "merge-products":
        merge = ProductMerge(service.store)
        if not args.apply:
            return merge.preview()
        if (
            not re.fullmatch(r"[a-f0-9]{64}", args.expected_sha256 or "")
            or not args.idempotency_key
        ):
            raise CatalogError(
                "Применение требует SHA-256 проверенного preview и ключ идемпотентности"
            )
        return merge.apply(args.expected_sha256, args.idempotency_key)
    if args.command in {"taxonomy-change", "taxonomy-change-apply", "taxonomy-singletons"}:
        from app.application.taxonomy_review import TaxonomyReview

        review = TaxonomyReview(service.store)
        if args.command == "taxonomy-change":
            return review.propose(
                json.loads(args.input.read_text(encoding="utf-8")),
                "operator_unverified",
                args.idempotency_key,
            )
        if args.command == "taxonomy-change-apply":
            return review.apply(
                args.change_id, args.expected_sha256, "operator_unverified", args.idempotency_key
            )
        return review.singletons()
    if args.command == "infrastructure-check":
        rabbit_status = service.queue.check()
        service.inference.check_availability()
        service.inference.ensure_revision()
        service.inference.ensure_revision(embedding=True)
        vector = service.inference.embed(["Проверка shared embedding"])[0]
        return {
            "rabbitmq": rabbit_status,
            "vlm_revision": service.inference.model_revision,
            "embedding_revision": service.inference.embedding_revision,
            "embedding_dimensions": len(vector),
        }
    if args.command == "import-file":
        if args.job_id:
            return service.control(args.job_id, "resume")
        if not args.input:
            raise CatalogError("Укажите --input либо --job-id")
        options = (
            json.loads(args.mapping_file.read_text(encoding="utf-8")) if args.mapping_file else {}
        )
        preview = service.upload(
            [(args.input.name, args.input)], {"profile": args.profile, **options}
        )
        return service.start(preview["job"]["id"], "cli-start:" + preview["job"]["id"])
    if args.command == "import-report":
        result = service.report(args.job_id)
        if args.summary:
            result.pop("items")
        return result
    if args.command == "reclassify":
        return service.reclassify(args.job_id, apply=args.apply, only_remaining=args.only_remaining)
    if args.command == "review-item":
        return service.decide(
            args.item_id,
            json.loads(args.decision_file.read_text(encoding="utf-8")),
            "operator_unverified",
            args.idempotency_key,
        )
    if args.command == "create-category":
        return service.create_category(
            json.loads(args.input.read_text(encoding="utf-8")),
            "operator_unverified",
            args.idempotency_key,
        )
    if args.command == "taxonomy-index-status":
        return service.index.status()
    if args.command == "taxonomy-reindex":
        if not args.apply:
            return {
                "dry_run": True,
                "categories": len(service.store.categories()),
                "index": service.index.status(),
            }
        return service.index.sync(force=args.force)
    if args.command == "live-smoke":
        if settings.inference_mode != "live" or not settings.live_smoke_enabled:
            raise CatalogError("Live smoke требует INFERENCE_MODE=live и LIVE_SMOKE_ENABLED=true")
        from app.domain.catalog import InputRow, evidence_bundle, normalize_row

        normalized = normalize_row(
            InputRow(0, 1, None, {}, {"name": "Выключатель концевой механический", "unit": "шт"})
        )
        answer = service.inference.answer(
            1,
            {
                "evidence_bundle_json": evidence_bundle(
                    "live-smoke",
                    normalized,
                    {"source": "synthetic_live_smoke"},
                    settings.inference_max_input_chars,
                )
            },
        )
        candidates, warnings = service.index.candidates(answer["answer_json"])
        selected = (
            service.inference.answer(
                2, {"answer_1_json": answer["answer_json"], "category_candidates_json": candidates}
            )
            if candidates
            else None
        )
        vector = service.inference.embed(["Категория: Электрика"])[0]
        return {
            "mode": "live",
            "answer_1": answer,
            "answer_2": selected,
            "dimensions": len(vector),
            "retrieval_warnings": warnings,
            "index": service.index.status(),
        }
    raise CatalogError("Неизвестная команда этапа 02")
