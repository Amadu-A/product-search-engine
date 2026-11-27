# app/application/product_merge.py
"""Предварительный план и атомарное объединение доказанных совпадений Product."""

from collections import defaultdict

from app.domain.catalog import CatalogError, ConflictError, fingerprint, json_value
from app.domain.product_identity import (
    accumulate_profile,
    identity_conflicts,
    identity_key,
    identity_profile,
)


def plan_products(rows: list[dict], manual: list[dict]) -> dict:
    """Строит группы по точным ключам и блокирует весь конфликтный набор."""
    products = {}
    for row in rows:
        key = str(row["product_id"])
        if key not in products:
            base = row.get("stored_profile") or identity_profile(
                {
                    "name": row["canonical_name"],
                    "brand": row["brand_name"],
                    "manufacturer": row["manufacturer_name"],
                    "mpn": row["mpn"],
                    "model": row["model"],
                    "unit": row["unit_code"],
                },
                row["stored_answer"],
            )
            products[key] = {
                "id": key,
                "name": row["canonical_name"],
                "created_at": row["created_at"],
                "category_id": row["category_id"],
                "version": row["updated_at"],
                "profile": base,
                "conflicts": [],
            }
        product = products[key]
        if row["fields"]:
            incoming = identity_profile(row["fields"], row["answer"] or row["stored_answer"])
            conflicts = identity_conflicts(product["profile"], incoming)
            if conflicts:
                product["conflicts"].extend(conflicts)
            else:
                product["profile"] = accumulate_profile(product["profile"], incoming)
    blocked_manual = {str(record["product_id"]) for record in manual if record["blocked"]}
    manual_categories = defaultdict(set)
    for record in manual:
        if category := record["category_id"]:
            manual_categories[str(record["product_id"])].add(category)
    by_key = defaultdict(list)
    for product in products.values():
        if match_key := identity_key(product["profile"]):
            by_key[match_key].append(product)
    groups = []
    blocked = []
    for key, members in by_key.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda p: (p["created_at"], p["id"]))
        survivor, *duplicates = members
        reasons = {reason for p in members for reason in p["conflicts"]}
        ids = {p["id"] for p in members}
        category_choices = {p["category_id"] for p in members if p["category_id"]}
        categories_manual = set().union(*(manual_categories[p["id"]] for p in members))
        if ids & blocked_manual or len(categories_manual) > 1:
            reasons.add("manual_decision")
        if len(category_choices | categories_manual) > 1:
            reasons.add("different_categories")
        profile = survivor["profile"]
        for other in duplicates:
            differences = identity_conflicts(profile, other["profile"])
            reasons.update(differences)
            if not differences:
                profile = accumulate_profile(profile, other["profile"])
        summary = {
            "key": key,
            "survivor": survivor["id"],
            "duplicates": [p["id"] for p in duplicates],
        }
        if reasons:
            blocked.append({**summary, "reasons": sorted(reasons)})
        else:
            groups.append(
                {
                    **summary,
                    "profile": profile,
                    "category_id": next(iter(category_choices), None),
                }
            )
    return json_value(
        {
            "products": list(products.values()),
            "groups": groups,
            "blocked": blocked,
            "insufficient": [
                p["id"]
                for p in products.values()
                if not identity_key(p["profile"]) or p["conflicts"]
            ],
        }
    )


class ProductMerge:
    """Повторно проверяет снимок, ручные решения и сохранность данных перед commit."""

    def __init__(self, store):
        """Использует SQL-репозиторий существующего каталога."""
        self.store = store

    def preview(self) -> dict:
        """Создаёт план без записи в БД и без запросов классификации."""
        with self.store.transaction() as unit:
            plan = self.plan(unit)
        return {**plan, "preview_sha256": fingerprint(plan)}

    def plan(self, unit) -> dict:
        """Читает связанные факты и сведения о ручных решениях из одного снимка."""
        rows = unit.product_identity_rows()
        return plan_products(rows, unit.product_identity_manual())

    def apply(self, expected: str, key: str) -> dict:
        """Переносит только группы из неизменившегося preview, сохраняя UUID и историю."""
        request = {"operation": "merge_products", "preview_sha256": expected}
        with self.store.transaction() as unit:
            cached = unit.command("operator_unverified", key, request)
            if cached is not None:
                return cached
            unit.lock_product_merge()
            plan = self.plan(unit)
            if fingerprint(plan) != expected:
                raise ConflictError(
                    "Данные или ручные решения изменились, сформируйте новый preview"
                )
            before = unit.merge_integrity()
            absorbed = {duplicate for group in plan["groups"] for duplicate in group["duplicates"]}
            enriched = 0
            for product in plan["products"]:
                if (
                    product["id"] not in absorbed
                    and not product["conflicts"]
                    and (product["profile"]["claims"] or identity_key(product["profile"]))
                ):
                    unit.remember_identity(product["id"], product["profile"])
                    enriched += 1
            transfers = {}
            for group in plan["groups"]:
                if group["category_id"] and not unit.assignable_path(group["category_id"]):
                    raise CatalogError(
                        "Категория группы больше не доступна. Объединение не применено."
                    )
                moved = unit.merge_products(group)
                for name, count in moved.items():
                    transfers[name] = transfers.get(name, 0) + count
            after = unit.merge_integrity()
            if after != before:
                raise ConflictError("Проверка сохранности исходных данных и прогресса не прошла")
            result = {
                "preview_sha256": expected,
                "groups": len(plan["groups"]),
                "retired": len(absorbed),
                "enriched": enriched,
                "blocked": len(plan["blocked"]),
                "moved": transfers,
                "integrity": after,
            }
            unit.save_command("operator_unverified", key, request, result)
        return result
