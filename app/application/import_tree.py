# app/application/import_tree.py
"""Use-case предварительного просмотра и отдельно подтверждённого атомарного apply."""

from app.application.ports import ImportUnitOfWork
from app.domain.planning import plan_import, require_no_conflicts
from app.domain.taxonomy import (
    Permission,
    Review,
    Snapshot,
    TaxonomyError,
    validate_review,
    validate_tree,
)
from app.observability import timed


class ImportTree:
    """Объединяет проверку дерева, прав, конфликтов и transaction boundary через UoW."""

    def __init__(self, uow: ImportUnitOfWork, max_nodes: int, max_depth: int) -> None:
        """Получает зависимости из composition root, не создаёт драйверов или клиентов."""
        self.uow = uow
        self.max_nodes = max_nodes
        self.max_depth = max_depth

    @timed("taxonomy_import")
    def execute(
        self,
        snapshot: Snapshot,
        *,
        apply: bool = False,
        review: Review | None = None,
        permission: Permission | None = None,
        commit_sha: str = "unknown",
    ) -> dict:
        """Возвращает dry-run либо фиксирует весь проверенный план одним commit.

        Apply требует Review точного SHA снимка и основания использования источника.
        Любой конфликт ручных правок отменяет всю операцию. Dry-run не пишет даже аудит.
        """
        metrics = validate_tree(snapshot, self.max_nodes, self.max_depth)
        if apply:
            snapshot.source.require_reuse(permission)
            if review is None:
                raise TaxonomyError("Apply требует --review с подтверждением полноты и выборки")
            validate_review(snapshot, review)
        with self.uow as transaction:
            plan = plan_import(snapshot, transaction.list_categories(), metrics)
            result = plan.counts()
            result["mode"] = "apply" if apply else "dry-run"
            if apply:
                require_no_conflicts(plan)
                result["run_id"] = transaction.persist(
                    snapshot, review, permission, plan, commit_sha
                )
                transaction.commit()
            return result
