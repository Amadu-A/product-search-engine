# app/application/category_selection.py
"""Политика выбора категории и ограниченный обход дерева из PostgreSQL."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from app.application.catalog_ports import CatalogStore, CategorySearch, Understanding
from app.domain.catalog import (
    DependencyUnavailable,
    InvalidModelOutput,
    category_purpose_conflict,
    json_value,
)

Event = Callable[[str, str, dict], None]


def candidate_view(row: dict) -> dict:
    """Передаёт актуальный путь и только утверждённую семантику."""
    return json_value(
        {
            "category_id": row["id"],
            **{
                key: row[key]
                for key in ("name", "path_ids", "path_names", "is_assignable", "review_status")
            },
            **{
                key: row.get("semantic_" + key) if row["review_status"] == "approved" else None
                for key in ("description", "purpose", "exclusions")
            },
        }
    )


@dataclass
class CategorySelection:
    """Итог выбора и журнал, который сохраняется вместе с SQL-решением."""

    category: dict | None = None
    selection_type: str = "none"
    strategy: str = "hybrid"
    reason: str = "Подходящая категория не найдена"
    unresolved: bool = False
    technical_failure: bool = False
    candidates: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


class HierarchicalCategorySelector:
    """На каждом уровне передаёт полный набор допустимых непосредственных детей."""

    def __init__(self, inference: Understanding, max_depth: int):
        """Получает готовый клиент и настроенное ограничение глубины."""
        self.inference = inference
        self.max_depth = max_depth

    def select(
        self, rows: list[dict], fields: dict, answer: dict, emit: Event
    ) -> CategorySelection:
        """Выбирает потомка или текущий узел; допускает один возврат к родителю."""
        output = CategorySelection(strategy="hierarchical", evidence={"hierarchical_steps": []})
        children = defaultdict(list)
        roots = {str(row["id"]) for row in rows if row["parent_id"] is None}
        available = {
            str(row["id"])
            for row in rows
            if row["is_active"] and row["review_status"] != "deprecated"
        }
        for row in rows:
            path = row["path_ids"]
            if (
                row["is_active"]
                and row["ancestors_active"]
                and row["review_status"] != "deprecated"
                and 0 < len(path) <= self.max_depth
                and len(set(path)) == len(path)
                and str(path[0]) in roots
                and set(map(str, path)) <= available
            ):
                children[str(row["parent_id"]) if row["parent_id"] else None].append(
                    candidate_view(row)
                )
        for siblings in children.values():
            siblings.sort(key=lambda row: (row["name"].casefold(), row["category_id"]))
        current = None
        stack = []
        excluded = set()
        backtracked = False
        while True:
            options = [
                child
                for child in children[current["category_id"] if current else None]
                if child["category_id"] not in excluded
            ]
            if not options and not (current and current["is_assignable"]):
                output.reason = "На уровне нет доступных категорий"
            else:
                try:
                    result = self.inference.answer(
                        3,
                        {
                            "normalized_record_json": {"name": fields["name"]},
                            "answer_1_json": answer,
                            "current_category_json": current,
                            "child_categories_json": options,
                        },
                    )
                except (InvalidModelOutput, DependencyUnavailable) as error:
                    output.unresolved = True
                    output.technical_failure = True
                    output.reason = str(error)
                    output.evidence["inference_issues"] = getattr(error, "attempts", [])
                    break
                reply = result["answer_json"]
                output.provenance = result
                path = current["path_names"] if current else []
                output.evidence["hierarchical_steps"].append(
                    {
                        "path": path,
                        "allowed_ids": [option["category_id"] for option in options],
                        "answer_3": reply,
                        "attempts": result.get("attempts", []),
                        "prompt_sha256": result["prompt_sha256"],
                        "model_revision": result["model_revision"],
                    }
                )
                output.reason = reply["reason"]
                if reply["action"] == "select_current":
                    if (
                        not current
                        or not current["is_assignable"]
                        or current["category_id"] != reply["category_id"]
                    ):
                        output.reason = "Текущая категория недоступна для назначения"
                        output.unresolved = True
                        return output
                    output.category = current
                    output.selection_type = "broader" if options else "exact"
                    output.candidates = [current]
                    emit("classification.level", "Подходит текущая категория", {"path": path})
                    return output
                if reply["action"] == "select_child":
                    selected = next(
                        (c for c in options if c["category_id"] == reply["category_id"]), None
                    )
                    if selected is None:
                        output.reason = "UUID модели вне допустимых потомков"
                        output.unresolved = True
                        return output
                    stack.append(current)
                    current = selected
                    emit("classification.level", "Выбрана ветка", {"path": current["path_names"]})
                    continue
            if not stack or backtracked:
                break
            excluded.add(current["category_id"])
            current = stack.pop()
            backtracked = True
            emit(
                "classification.backtrack",
                "Ветка не подошла, проверяю альтернативу",
                {"path": current["path_names"] if current else []},
            )
        return output


class CategoryClassifier:
    """Общий выбор для импорта, повторной классификации и сравнительного прогона."""

    def __init__(
        self, store: CatalogStore, index: CategorySearch, inference: Understanding, depth: int
    ):
        """Получает готовые адаптеры SQL, гибридного поиска и VLM."""
        self.store, self.index, self.inference = store, index, inference
        self.hierarchy = HierarchicalCategorySelector(inference, depth)

    def select(self, fields, answer, emit: Event, strategy="combined") -> CategorySelection:
        """Сравниваемые стратегии используют одинаковые контракты валидации."""
        result = self.select_strategy(fields, answer, emit, strategy)
        if result.category:
            conflict = category_purpose_conflict(fields["name"], result.category["path_names"])
            if conflict:
                result.evidence["purpose_conflict"] = {
                    "rejected_category": result.category,
                    "reason": conflict,
                }
                result.category = None
                result.selection_type = "none"
                result.reason = conflict
                result.unresolved = True
        return result

    def select_strategy(self, fields, answer, emit: Event, strategy: str) -> CategorySelection:
        """Выбирает исходного кандидата по одной стратегии до проверки назначения."""
        if strategy == "hierarchical":
            return self.run_hierarchy(fields, answer, emit)
        hybrid = self.hybrid(answer, emit)
        if hybrid.category is not None or strategy == "hybrid":
            return hybrid
        fallback = self.run_hierarchy(fields, answer, emit)
        fallback.evidence = {
            "hybrid": {"reason": hybrid.reason, "candidates": hybrid.candidates, **hybrid.evidence},
            **fallback.evidence,
        }
        return fallback

    def run_hierarchy(self, fields, answer, emit: Event) -> CategorySelection:
        """Снимает согласованный SQL-снимок перед сетевыми вызовами."""
        emit("classification.fallback", "Поиск по дереву PostgreSQL", {})
        _, rows = self.store.index_snapshot()
        return self.hierarchy.select(rows, fields, answer, emit)

    def hybrid(self, answer, emit: Event) -> CategorySelection:
        """Требует решения VLM по свежему candidate-set, включая широкие категории."""
        candidates, warnings = self.index.candidates(answer)
        result = CategorySelection(candidates=candidates, evidence={"warnings": warnings})
        emit(
            "classification.candidates",
            f"Гибридный поиск: {len(candidates)} кандидатов",
            {"paths": [c["path_names"] for c in candidates], "warnings": warnings},
        )
        if warnings or not candidates:
            result.reason = "Индекс недоступен или отстаёт" if warnings else result.reason
            result.unresolved = bool(warnings)
            return result
        try:
            result.provenance = self.inference.answer(
                2, {"answer_1_json": answer, "category_candidates_json": candidates}
            )
        except (InvalidModelOutput, DependencyUnavailable) as error:
            result.unresolved = True
            result.technical_failure = True
            result.reason = str(error)
            result.evidence["inference_issues"] = getattr(error, "attempts", [])
            return result
        reply = result.provenance["answer_json"]
        result.evidence.update(
            answer_2=reply, inference_issues=result.provenance.get("attempts", [])
        )
        result.reason = reply["reason"]
        result.unresolved = reply["status"] == "needs_review"
        if reply["status"] == "selected":
            result.category = next(
                (c for c in candidates if c["category_id"] == reply["category_id"]), None
            )
            if result.category is None:
                result.reason = "UUID модели вне candidate-set"
                result.unresolved = True
                return result
            result.selection_type = reply["selection_type"]
        return result
