# app/domain/quality.py
"""Информационная диагностика полноты и качества названий без запрета импорта дерева."""

import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.taxonomy import Snapshot

PRICE = re.compile(r"(?:\d[\d\s\u00a0.,]*\s*(?:₽|руб\b|руб\.|RUB\b)|от\s*\d)", re.I)
COLLECTION = re.compile(r"\bколлекци[а-яё]*\b", re.I)


def category_quality_flags(name: str, path: tuple[str, ...]) -> list[str]:
    """Помечает цену и признаки коллекции, не меняя название, активность или идентификатор."""
    flags = []
    has_price = bool(PRICE.search(name))
    if has_price:
        flags.append("price_in_name")
    if any(COLLECTION.search(part) for part in path) or (
        has_price and re.search(r"\([^()]+\)", name)
    ):
        flags.append("suspected_product_collection")
    return flags


def tree_diagnostics(snapshot: "Snapshot") -> dict:
    """Отделяет предупреждения сборщика и подозрительные записи от структурных ошибок.

    Отсутствующий отчёт означает неизвестную полноту. Ошибки/redirect не суммируются
    с remaining_pages: наборы могут пересекаться. Условия использования информационны.
    """
    details = snapshot.diagnostics or {}
    collector = details.get("collector_report")
    warnings = []
    if not snapshot.complete:
        warnings.append({"code": "incomplete_tree", "message": "Исходное дерево неполное"})
    if collector is None:
        collection = {"report_provided": False}
    else:
        remaining = collector.get("remaining_pages", 0)
        errors = collector.get("errors", [])
        redirects = collector.get("redirects", [])
        collection = {
            "report_provided": True,
            "remaining_pages": remaining,
            "errors": len(errors),
            "redirects": len(redirects),
            "fetched_pages": collector.get("fetched_pages"),
            "input_complete": collector.get("input_complete"),
            "stop_reason": collector.get("stop_reason"),
        }
        for code, count in (
            ("unprocessed_pages", remaining),
            ("collector_errors", len(errors)),
            ("collector_redirects", len(redirects)),
        ):
            if count:
                warnings.append({"code": code, "count": count})
        by_url = {n.source_url: n for n in snapshot.nodes}
        collection["affected_branches"] = [
            {
                "url": entry.get("url"),
                "path": list(by_url[entry.get("url")].path) if entry.get("url") in by_url else None,
            }
            for entry in (*errors, *redirects)
            if isinstance(entry, dict)
        ]
        if remaining:
            collection["remaining_paths_known"] = False
        if collector.get("input_complete") is False and snapshot.complete:
            warnings.append({"code": "collector_completeness_disagrees"})
        if collector.get("categories") not in (None, len(snapshot.nodes)):
            warnings.append({"code": "collector_category_count_disagrees"})
    suspicious = []
    flag_counts = Counter()
    for node in snapshot.nodes:
        flags = category_quality_flags(node.name, node.path)
        if flags:
            flag_counts.update(flags)
            suspicious.append(
                {
                    "id": str(node.id),
                    "external_id": node.external_id,
                    "name": node.name,
                    "path": list(node.path),
                    "source_url": node.source_url,
                    "quality_flags": flags,
                    "quality_status": "needs_review",
                }
            )
    if suspicious:
        warnings.append({"code": "suspected_categories", "count": len(suspicious)})
    return {
        "complete": snapshot.complete,
        "warnings": warnings,
        "collection": collection,
        "quality": {
            "suspected_count": len(suspicious),
            "flag_counts": dict(flag_counts),
            "suspected_nodes": suspicious,
            "unflagged_status": "unverified",
        },
        "input_sha256": details.get("input_sha256"),
        "normalizations": details.get("normalizations", []),
        "source_information": snapshot.source.model_dump(mode="json"),
    }
