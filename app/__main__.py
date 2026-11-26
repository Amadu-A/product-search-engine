# app/__main__.py
"""Тонкий CLI с явным apply; рабочая БД не заполняется автоматически."""

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.bootstrap import build_import, build_report
from app.domain.catalog import CatalogError, DependencyUnavailable
from app.domain.taxonomy import (
    Permission,
    Review,
    Snapshot,
    TaxonomyError,
    snapshot_hash,
    validate_tree,
)
from app.infrastructure.database import check_database, create_db_engine
from app.infrastructure.files import load_snapshot, normalize_file, read_limited, write_json
from app.infrastructure.petrovich import PetrovichAdapter
from app.infrastructure.quality_results import read_test_results
from app.settings import Settings


def parser() -> argparse.ArgumentParser:
    """Объявляет CLI-контракт без скрытых операций записи и комбинированного auto-apply."""
    root = argparse.ArgumentParser(description="Технический импорт локального дерева категорий")
    commands = root.add_subparsers(dest="command", required=True)
    export = commands.add_parser(
        "export", help="Экспорт законного вложенного JSON/HTML в плоский JSON"
    )
    export.add_argument("--input", type=Path, required=True)
    export.add_argument("--metadata", type=Path)
    export.add_argument("--collector-report", type=Path)
    importing = commands.add_parser(
        "import", help="Проверка/preview локального JSON; --apply записывает БД"
    )
    importing.add_argument("--input", type=Path, required=True)
    importing.add_argument("--collector-report", type=Path)
    importing.add_argument("--apply", action="store_true")
    for name in ("validate", "preview", "apply", "review-template"):
        command = commands.add_parser(name)
        command.add_argument("--snapshot", type=Path)
        if name == "apply":
            command.add_argument("--review", type=Path)
    commands.add_parser("report")
    commands.add_parser("db-check")
    commands.add_parser("fetch-petrovich", help="Один разрешённый каталог, без массового обхода")
    commands.add_parser("serve", help="HTTP UI и Swagger")
    worker = commands.add_parser("worker", help="Durable ingestion и index worker")
    worker.add_argument("--once", action="store_true")
    file_import = commands.add_parser("import-file", help="Общий с UI импорт номенклатуры")
    file_import.add_argument("--input", type=Path)
    file_import.add_argument("--profile", default="auto")
    file_import.add_argument("--mapping-file", type=Path)
    file_import.add_argument("--job-id")
    mode = file_import.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    for name in (
        "taxonomy-index-status",
        "taxonomy-reindex",
        "taxonomy-singletons",
        "live-smoke",
        "infrastructure-check",
    ):
        extra = commands.add_parser(name)
        if name == "taxonomy-reindex":
            modes = extra.add_mutually_exclusive_group()
            modes.add_argument("--apply", action="store_true")
            modes.add_argument("--dry-run", action="store_true")
            extra.add_argument("--force", action="store_true")
    report = commands.add_parser("import-report")
    report.add_argument("--job-id", required=True)
    report.add_argument("--summary", action="store_true")
    reclassify = commands.add_parser("reclassify", help="Повторная классификация записей задания")
    reclassify.add_argument("--job-id", required=True)
    reclassify.add_argument("--apply", action="store_true")
    decision = commands.add_parser("review-item")
    decision.add_argument("--item-id", required=True)
    decision.add_argument("--decision-file", type=Path, required=True)
    decision.add_argument("--idempotency-key", required=True)
    category = commands.add_parser("create-category")
    category.add_argument("--input", type=Path, required=True)
    category.add_argument("--idempotency-key", required=True)
    change = commands.add_parser("taxonomy-change")
    change.add_argument("--input", type=Path, required=True)
    change.add_argument("--idempotency-key", required=True)
    apply_change = commands.add_parser("taxonomy-change-apply")
    apply_change.add_argument("--change-id", required=True)
    apply_change.add_argument("--expected-sha256", required=True)
    apply_change.add_argument("--idempotency-key", required=True)
    return root


def optional_permission(settings: Settings) -> Permission | None:
    """Читает только существующее основание; не создаёт и не предполагает разрешений."""
    if not settings.permission_file.exists():
        return None
    return Permission.model_validate_json(read_limited(settings.permission_file, 1048576))


def review_template(snapshot: Snapshot, settings: Settings) -> dict:
    """Создаёт неутверждённый шаблон с выборкой каждой ветви для ручной сверки."""
    nodes = {n.id: n for n in snapshot.nodes}
    samples = {}
    for node in sorted(snapshot.nodes, key=lambda n: n.level):
        current = node
        while current.parent_id:
            current = nodes[current.parent_id]
        samples[current.id] = {
            "id": str(node.id),
            "name": node.name,
            "path": list(node.path),
            "source_url": node.source_url,
        }
    result = {
        "snapshot_sha256": snapshot_hash(snapshot),
        "reviewed_by": "",
        "reviewed_at": None,
        "reference": "",
        "complete": False,
        "samples": list(samples.values()),
    }
    target = settings.artifact_dir / "review.template.json"
    write_json(target, result)
    return {"template": str(target), "approved": False}


def dispatch(args: argparse.Namespace, settings: Settings) -> dict:
    """Преобразует CLI-аргументы в вызов одного application use-case."""
    if args.command in {
        "serve",
        "worker",
        "import-file",
        "import-report",
        "reclassify",
        "review-item",
        "create-category",
        "taxonomy-index-status",
        "infrastructure-check",
        "taxonomy-change",
        "taxonomy-change-apply",
        "taxonomy-reindex",
        "taxonomy-singletons",
        "live-smoke",
    }:
        from app.catalog_cli import dispatch_catalog

        return dispatch_catalog(args, settings)
    if args.command in {"export", "import"}:
        snapshot = normalize_file(
            args.input,
            settings.artifact_dir,
            settings.http_max_response_bytes,
            getattr(args, "metadata", None),
            settings.taxonomy_max_nodes,
            settings.taxonomy_max_depth,
            collector_report_path=args.collector_report,
        )
        if args.command == "import":
            return build_import(settings, apply=args.apply).execute(
                snapshot,
                apply=args.apply,
                commit_sha=settings.vcs_ref,
            )
        return {
            "snapshot": str(settings.artifact_dir / "categories.json"),
            "sha256": snapshot_hash(snapshot),
            "nodes": len(snapshot.nodes),
            "complete": snapshot.complete,
            "normalization_report": str(settings.artifact_dir / "normalization_report.json"),
            "warnings": validate_tree(
                snapshot, settings.taxonomy_max_nodes, settings.taxonomy_max_depth
            )["warnings"],
        }
    if args.command == "db-check":
        engine = create_db_engine(settings)
        try:
            return check_database(engine)
        finally:
            engine.dispose()
    if args.command == "fetch-petrovich":
        permission = optional_permission(settings)
        if permission is None:
            raise TaxonomyError("BLOCKED: документированного разрешения Петровича нет")
        path = PetrovichAdapter(settings).fetch_catalog(permission, settings.artifact_dir / "raw")
        return {"file": str(path), "completeness": "unverified", "imported": False}
    if args.command == "report":
        attempt = settings.report_file.parent / "last_attempt.json"
        context = {
            "last_attempt": json.loads(attempt.read_text(encoding="utf-8"))
            if attempt.exists()
            else None,
            "test_results": read_test_results(settings.report_file.parent),
            "petrovich": "Массовый обход отключён; условия конкретного источника отражены в аудите",
        }
        content = build_report(settings).execute(settings.vcs_ref, context)
        settings.report_file.parent.mkdir(parents=True, exist_ok=True)
        settings.report_file.write_text(content, encoding="utf-8")
        print(content)
        return {"report": str(settings.report_file)}
    snapshot = load_snapshot(
        args.snapshot or settings.artifact_dir / "categories.json",
        settings.snapshot_max_bytes,
    )
    if args.command == "validate":
        return validate_tree(snapshot, settings.taxonomy_max_nodes, settings.taxonomy_max_depth)
    if args.command == "review-template":
        return review_template(snapshot, settings)
    applying = args.command == "apply"
    review = (
        Review.model_validate_json(read_limited(args.review, 1048576))
        if applying and args.review
        else None
    )
    return build_import(settings, apply=applying).execute(
        snapshot,
        apply=applying,
        review=review,
        commit_sha=settings.vcs_ref,
    )


def main(argv: list[str] | None = None) -> int:
    """Возвращает ненулевой код при блокировке, скрывая credentials из ошибок драйвера."""
    args = parser().parse_args(argv)
    settings = None
    try:
        settings = Settings()
        logging.basicConfig(
            level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s"
        )
        result = dispatch(args, settings)
    except (
        TaxonomyError,
        CatalogError,
        DependencyUnavailable,
        OSError,
        ValueError,
        SQLAlchemyError,
    ) as error:
        if isinstance(error, (TaxonomyError, CatalogError, DependencyUnavailable)):
            message = str(error)
        elif isinstance(error, ValidationError):
            message = "Неверный контракт/настройки; поля: " + ", ".join(
                ".".join(str(p) for p in e["loc"]) for e in error.errors(include_input=False)
            )
        else:
            message = (
                f"Операция не выполнена ({type(error).__name__}); проверьте файл/БД/конфигурацию"
            )
        result = {"status": "BLOCKED", "command": args.command, "reason": message}
        write_json(
            (settings.report_file.parent if settings else Path(".tmp/reports"))
            / "last_attempt.json",
            {
                **result,
                "date": datetime.now(UTC).isoformat(),
            },
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    if args.command in {"import", "apply", "preview", "export", "fetch-petrovich"}:
        write_json(
            settings.report_file.parent / "last_attempt.json",
            {
                "status": "success",
                "command": args.command,
                "date": datetime.now(UTC).isoformat(),
                "result": result,
            },
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
