# app/application/category_selection.py
"""Политика выбора категории и ограниченный обход дерева из PostgreSQL."""

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from app.application.catalog_ports import CatalogStore, CategorySearch, Understanding
from app.domain.catalog import (
    DependencyUnavailable,
    InvalidModelOutput,
    approved_category_exclusion,
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
                for key in (
                    "name",
                    "path_ids",
                    "path_names",
                    "is_active",
                    "is_assignable",
                    "review_status",
                )
            },
            "ancestor_exclusions": row.get("ancestor_exclusions", []),
            "aliases": row.get("aliases", []),
            "review_status_allowed": row["review_status"] in {"approved", "provisional"},
            **{
                key: row.get("semantic_" + key) if row["review_status"] == "approved" else None
                for key in ("description", "purpose", "exclusions")
            },
        }
    )


def suggested_category_ids(decision: dict) -> list[str]:
    """Извлекает только UUID, которые VLM выбрала или сравнила, и учитывает прямые отказы."""
    evidence = decision["evidence"]
    hybrid = evidence.get("hybrid", evidence)
    ranked: list[str] = []
    rejected = set(evidence.get("semantic_check_declined_ids", []))
    if decision["status"] == "proposed" and decision["category_id"]:
        ranked.append(str(decision["category_id"]))
    for reply, candidates in (
        (hybrid.get("answer_2", {}), hybrid.get("candidates", decision["candidates"])),
        (evidence.get("semantic_check", {}), evidence.get("semantic_check_candidates", [])),
    ):
        allowed = {c["category_id"] for c in candidates}
        if reply.get("status") == "selected" and reply["category_id"] in allowed:
            ranked.append(reply["category_id"])
        elif reply.get("status") == "needs_review":
            ranked.extend(key for key in reply.get("compared_candidate_ids", []) if key in allowed)
        elif reply.get("status") == "no_suitable_category":
            rejected.update(allowed)
    steps = evidence.get("hierarchical_steps", [])
    if steps:
        step = steps[-1]
        reply = step["answer_3"]
        selected = reply.get("category_id")
        if reply["action"] == "select_child" and selected in step["allowed_ids"]:
            ranked.insert(0, selected)
        if reply["action"] == "select_current" and any(
            selected in arrival["allowed_ids"] and selected == arrival["answer_3"]["category_id"]
            for arrival in steps[:-1]
        ):
            ranked.insert(0, selected)
    if evidence.get("semantic_check", {}).get("status") == "no_suitable_category":
        conflict = evidence.get("purpose_conflict", {}).get("rejected_category")
        if conflict:
            rejected.add(conflict["category_id"])
    return [key for key in dict.fromkeys(ranked) if key not in rejected]


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
        self, rows: list[dict], fields: dict, answer: dict, emit: Event, anchor: dict | None = None
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
        current = anchor
        stack = []
        excluded = set()
        backtracked = False
        while True:
            options = [
                child
                for child in children[current["category_id"] if current else None]
                if child["category_id"] not in excluded
                and not approved_category_exclusion(fields["name"], child)
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
        """Проверяет гибридный результат до перехода к обходу или уточнению родителя."""
        if strategy == "hierarchical":
            return self.run_hierarchy(fields, answer, emit)
        hybrid = self.hybrid(answer, emit)
        self.check_exclusions(hybrid, fields)
        if strategy == "hybrid":
            return hybrid
        if hybrid.category is not None:
            return self.refine_broader(hybrid, fields, answer, emit)
        fallback = self.run_hierarchy(fields, answer, emit)
        fallback.evidence = {
            "hybrid": {"reason": hybrid.reason, "candidates": hybrid.candidates, **hybrid.evidence},
            **fallback.evidence,
        }
        return fallback

    def check_exclusions(self, result: CategorySelection, fields: dict) -> None:
        """Отклоняет выбранный UUID по утверждённому исключению из SQL."""
        if result.category and (
            phrase := approved_category_exclusion(fields["name"], result.category)
        ):
            self.reject_selection(result, f"Утверждённое исключение категории: {phrase}")

    def reject_selection(self, result: CategorySelection, reason: str) -> None:
        """Сохраняет отклонённый вариант для объяснения fallback."""
        result.evidence["purpose_conflict"] = {
            "rejected_category": result.category,
            "reason": reason,
        }
        result.category = None
        result.selection_type = "none"
        result.reason = reason
        result.unresolved = True

    def refine_broader(self, hybrid, fields, answer, emit: Event) -> CategorySelection:
        """Даёт модели выбрать из непосредственных потомков широкого назначения."""
        if hybrid.selection_type != "broader":
            return hybrid
        _, rows = self.store.index_snapshot()
        anchor = hybrid.category
        if not any(
            str(row["parent_id"]) == anchor["category_id"]
            and row["is_active"]
            and row["review_status"] != "deprecated"
            for row in rows
        ):
            return hybrid
        emit(
            "classification.refine",
            "Проверяю подкатегории широкого выбора",
            {"path": anchor["path_names"]},
        )
        refinement = self.hierarchy.select(rows, fields, answer, emit, anchor)
        if refinement.category and refinement.category["category_id"] != anchor["category_id"]:
            self.verify_hierarchy(refinement, fields, answer, rows)
            if refinement.category and refinement.category["category_id"] != anchor["category_id"]:
                refinement.evidence["hybrid"] = {"candidates": hybrid.candidates, **hybrid.evidence}
                return refinement
        hybrid.evidence["broader_refinement"] = {"reason": refinement.reason, **refinement.evidence}
        return hybrid

    def verify_hierarchy(self, result, fields, answer, rows=()) -> CategorySelection:
        """Проверяет выбор обхода и отдельно рассматривает пригодного родителя при отказе."""
        self.check_exclusions(result, fields)
        if result.category is None:
            return result
        category = result.category
        parents = category.get("path_ids", [])[1:-1]
        by_id = {str(row["id"]): row for row in rows if str(row["id"]) in parents}
        options = [category] + [
            candidate_view(by_id[key]) for key in reversed(parents) if key in by_id
        ]
        options = [
            option
            for option in options
            if option.get("is_assignable", True)
            and not approved_category_exclusion(fields["name"], option)
            and self.has_scope_evidence(fields["name"], answer, option)
        ]
        if not options:
            self.reject_selection(result, "Исходные сведения не подтверждают назначение ветки")
            return result
        batches = [[category], options[1:]] if options[0] is category else [options]
        for batch in filter(None, batches):
            result.evidence["semantic_check_candidates"] = batch
            try:
                check = self.inference.answer(
                    2,
                    {
                        "answer_1_json": {**answer, "source_product_name": fields["name"]},
                        "category_candidates_json": batch,
                    },
                )
            except (InvalidModelOutput, DependencyUnavailable) as error:
                self.reject_selection(result, str(error))
                result.technical_failure = True
                result.evidence.setdefault("semantic_check_issues", []).extend(
                    getattr(error, "attempts", [])
                )
                return result
            reply = check["answer_json"]
            result.evidence["semantic_check"] = reply
            result.evidence.setdefault("semantic_check_issues", []).extend(
                check.get("attempts", [])
            )
            result.reason = reply["reason"]
            selected = next((c for c in batch if c["category_id"] == reply["category_id"]), None)
            if reply["status"] == "selected" and selected is not None:
                result.category = selected
                if selected is not category:
                    result.selection_type = "broader"
                result.candidates = [selected]
                return result
            if reply["status"] == "no_suitable_category":
                result.evidence.setdefault("semantic_check_declined_ids", []).extend(
                    c["category_id"] for c in batch
                )
        self.reject_selection(result, result.reason)
        return result

    def has_scope_evidence(self, name: str, answer: dict, category: dict) -> bool:
        """Останавливает догадку о назначении, когда ни изделие, ни ветка не описывают его."""
        if answer.get("purpose") or answer.get("applications"):
            return True
        if category.get("description") or category.get("purpose"):
            return True
        branch = " ".join([*category["path_names"], *category.get("aliases", [])])
        return bool(self.scope_tokens(name) & self.scope_tokens(branch))

    @staticmethod
    def scope_tokens(text: str) -> set[str]:
        """Сопоставляет основы, в том числе формы с беглой гласной в суффиксе -ок/-ек."""
        return {
            re.sub(r"[ое]к$", "к", word)[:4]
            for word in re.findall(r"[^\W\d_]{4,}", text.casefold())
        }

    def run_hierarchy(self, fields, answer, emit: Event) -> CategorySelection:
        """Снимает согласованный SQL-снимок перед сетевыми вызовами."""
        emit("classification.fallback", "Поиск по дереву PostgreSQL", {})
        _, rows = self.store.index_snapshot()
        result = self.hierarchy.select(rows, fields, answer, emit)
        return self.verify_hierarchy(result, fields, answer, rows)

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
