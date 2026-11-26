# app/application/taxonomy_review.py
"""Явные changesets одиночных ветвей; ни один узел не удаляется автоматически."""

from datetime import UTC, datetime
from uuid import UUID

from app.domain.catalog import CatalogError, ConflictError, fingerprint, json_value


class TaxonomyReview:
    """Предварительный план и отдельное утверждение сохраняют UUID и исторические ссылки."""

    def __init__(self, store):
        """Использует ту же PostgreSQL схему/outbox, что основной импорт."""
        self.store = store

    def singletons(self) -> dict:
        """Сверяет реальные активные ветви вместо старого кандидатного отчёта."""
        rows = self.store.categories()
        children = {}
        for row in rows:
            if row["is_active"]:
                children.setdefault(row["parent_id"], []).append(row)
        return {
            "mutated": False,
            "singletons": [
                {
                    "parent_id": str(r["id"]),
                    "child_id": str(children[r["id"]][0]["id"]),
                    "path": r["path_names"],
                    "requires_review": True,
                }
                for r in rows
                if r["is_active"] and len(children.get(r["id"], [])) == 1
            ],
        }

    def plan(self, values: dict) -> dict:
        """Вычисляет scope переназначения и hash актуального SQL-дерева без записи."""
        rows = self.store.categories()
        by_id = {str(r["id"]): r for r in rows}
        old_id = str(values["category_id"])
        action = values["action"]
        if old_id not in by_id or not by_id[old_id]["is_active"]:
            raise CatalogError("Категория changeset не найдена или не активна")
        old = by_id[old_id]
        target_id = str(values["target_category_id"]) if values.get("target_category_id") else None
        if action == "remove_child":
            target_id = str(old["parent_id"]) if old["parent_id"] else None
            siblings = [r for r in rows if r["parent_id"] == old["parent_id"] and r["is_active"]]
            if len(siblings) != 1:
                raise CatalogError("remove_child разрешён только для единственного ребёнка")
        elif action == "remove_parent_reparent":
            children = [r for r in rows if r["parent_id"] == old["id"] and r["is_active"]]
            if len(children) != 1:
                raise CatalogError("Ожидался единственный активный ребёнок")
            target_id = str(children[0]["id"])
        elif action not in {"keep", "merge", "deactivate"}:
            raise CatalogError("Неизвестный action changeset")
        if action not in {"keep", "deactivate"} and not target_id:
            raise CatalogError("Не задана сохраняемая категория")
        if target_id:
            target = by_id.get(target_id)
            if (
                not target
                or target_id == old_id
                or not target["is_active"]
                or not target["ancestors_active"]
                or not target["is_assignable"]
                or target["review_status"] == "deprecated"
            ):
                raise CatalogError("Целевая категория недоступна")
            if action != "remove_parent_reparent" and old_id in target["path_ids"]:
                raise CatalogError("Перемещение создало бы цикл")
        children = [r for r in rows if r["parent_id"] == old["id"] and r["is_active"]]
        if action == "deactivate" and children:
            raise CatalogError("Сначала разберите дочерние ветви отдельным changeset")
        with self.store.transaction() as unit:
            refs = unit.category_references(UUID(old_id))
            active_products = unit.active_category_products(UUID(old_id))
        if action == "deactivate" and not target_id and active_products:
            raise CatalogError("У категории есть активные товары. Укажите категорию для переноса.")
        signature = [
            {
                k: r[k]
                for k in (
                    "id",
                    "parent_id",
                    "name",
                    "is_active",
                    "is_assignable",
                    "review_status",
                    "updated_at",
                    "product_count",
                )
            }
            for r in sorted(rows, key=lambda r: str(r["id"]))
        ]
        return json_value(
            {
                "action": action,
                "category_id": old_id,
                "target_category_id": target_id,
                "old_path": old["path_names"],
                "references": refs,
                "active_products": active_products,
                "tree_sha256": fingerprint(signature),
                "reference_sha256": fingerprint(refs),
                "physical_delete": False,
                "index_status": "sync_pending_after_apply",
            }
        )

    def propose(self, values: dict, actor: str, key: str) -> dict:
        """Сохраняет review plan; предложение не меняет дерево."""
        if not values.get("reason", "").strip():
            raise CatalogError("Укажите основание changeset")
        plan = self.plan(values)
        with self.store.transaction() as unit:
            cached = unit.command(actor, key, values)
            if cached:
                return cached
            change = unit.add(
                "category_change_sets",
                action=values["action"],
                category_id=UUID(plan["category_id"]),
                target_category_id=UUID(plan["target_category_id"])
                if plan["target_category_id"]
                else None,
                reason=values["reason"],
                preview=plan,
            )
            response = json_value(change)
            response["preview_sha256"] = fingerprint(plan)
            unit.save_command(actor, key, values, response)
        return response

    def apply(self, change_id, expected_sha256: str, actor: str, key: str) -> dict:
        """Утверждает конкретный preview и атомарно переназначает ссылки с redirect/outbox."""
        request = {"change_id": str(change_id), "expected_sha256": expected_sha256}
        with self.store.transaction() as unit:
            cached = unit.command(actor, key, request)
            if cached:
                return cached
            unit.lock_taxonomy_change()
            change = unit.get("category_change_sets", change_id, lock=True)
            if change["status"] == "applied":
                return json_value(change)
            if change["status"] not in {"proposed", "approved"}:
                raise ConflictError("Changeset не доступен для применения")
            current = self.plan(change)
            if (
                fingerprint(current) != fingerprint(change["preview"])
                or fingerprint(current) != expected_sha256
            ):
                raise ConflictError(
                    "Дерево или ссылки изменились после preview; создайте новый changeset"
                )
            unit.apply_taxonomy_change(change, actor)
            unit.update(
                "category_change_sets",
                change["id"],
                status="applied",
                approved_by=actor,
                applied_at=datetime.now(UTC),
            )
            result = {"id": str(change["id"]), "status": "applied", "sync_status": "sync_pending"}
            unit.save_command(actor, key, request, result)
        return result
