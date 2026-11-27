# app/application/product_merge.py
"""Предварительный план и атомарное объединение доказанных совпадений Product."""

from collections import Counter, defaultdict

from app.domain.catalog import CatalogError, ConflictError, fingerprint, json_value
from app.domain.product_identity import (
    IDENTITY_LIMIT,
    accumulate_profile,
    canonical_brand,
    decide_identity,
    identity_conflicts,
    identity_key,
    identity_profile,
    identity_proof,
    identity_retrieval_keys,
    refine_legacy_roles,
    source_locator,
    with_verified_reference,
)


def plan_products(rows: list[dict], manual: list[dict], references=(), aliases=None) -> dict:
    """Строит группы по доказуемым связям и проверяет совместимость всех фактов."""
    products = {}
    aliases = aliases or {}
    rows = sorted(
        rows,
        key=lambda row: (row["created_at"], str(row["product_id"]), str(row.get("raw_id") or "")),
    )
    for row in rows:
        key = str(row["product_id"])
        if key not in products:
            stored = row.get("stored_profile")
            base = identity_profile(
                {
                    "name": row["canonical_name"],
                    **(
                        {
                            "brand": row["brand_name"],
                            "manufacturer": row["manufacturer_name"],
                            "mpn": row["mpn"],
                            "model": row["model"],
                        }
                        if not stored
                        and not row["stored_answer"]
                        and row.get("product_status") == "identified"
                        else {}
                    ),
                },
                row["stored_answer"],
            )
            for reference in references:
                base = with_verified_reference(base, reference)
                if stored:
                    stored = with_verified_reference(
                        {**stored, "name": row["canonical_name"]}, reference
                    )
            base = canonical_brand(base, aliases)
            if stored:
                stored = canonical_brand(stored, aliases)
            products[key] = {
                "id": key,
                "name": row["canonical_name"],
                "created_at": row["created_at"],
                "category_id": row["category_id"],
                "version": row["updated_at"],
                "profile": accumulate_profile(stored, base)
                if stored and not identity_conflicts(stored, base)
                else stored or base,
                "conflicts": identity_conflicts(stored, base) if stored else [],
                "sources": [],
                "category_weight": row.get("category_weight", 0),
            }
        product = products[key]
        if row["fields"]:
            incoming = identity_profile(
                {**row["fields"], "origin_url": row.get("origin_url")},
                row["answer"] or row["stored_answer"],
            )
            for reference in references:
                incoming = with_verified_reference(incoming, reference)
            incoming = canonical_brand(incoming, aliases)
            known = refine_legacy_roles(product["profile"], incoming)
            incoming = refine_legacy_roles(incoming, known)
            conflicts = identity_conflicts(known, incoming)
            if conflicts:
                product["conflicts"].extend(conflicts)
            else:
                product["profile"] = accumulate_profile(known, incoming)
            if row.get("raw_id"):
                product["sources"].append(
                    {
                        field: row.get(field)
                        for field in (
                            "raw_id",
                            "offer_id",
                            "seller_id",
                            "seller_name",
                            "origin_url",
                            "item_status",
                        )
                    }
                )
    blocked_manual = {str(record["product_id"]) for record in manual if record["blocked"]}
    manual_categories = defaultdict(set)
    for record in manual:
        if category := record["category_id"]:
            manual_categories[str(record["product_id"])].add(category)
    by_key = defaultdict(list)
    for product in products.values():
        for key in identity_retrieval_keys(product["profile"]):
            if len(by_key[key]) <= IDENTITY_LIMIT:
                by_key[key].append(product["id"])
    links, proofs = defaultdict(set), defaultdict(set)
    blocked = []
    for pid, product in products.items():
        candidates = set()
        for key in identity_retrieval_keys(product["profile"]):
            candidates.update(by_key[key])
        candidates.discard(pid)
        if len(candidates) > IDENTITY_LIMIT:
            blocked_manual.add(pid)
            blocked.append({"survivor": pid, "duplicates": [], "reasons": ["candidate_limit"]})
            continue
        for candidate in sorted(candidates):
            if candidate >= pid:
                continue
            other = products[candidate]
            if proof := identity_proof(product["profile"], other["profile"]):
                links[pid].add(candidate)
                links[candidate].add(pid)
                proofs[pid].add(proof)
                proofs[candidate].add(proof)
            else:
                reasons = identity_conflicts(product["profile"], other["profile"])
                blocked.append(
                    {
                        "survivor": candidate,
                        "duplicates": [pid],
                        "reasons": reasons or ["insufficient_evidence"],
                    }
                )
    groups = []
    visited = set()
    for pid in products:
        if pid in visited or not links[pid]:
            continue
        component, stack = {pid}, [pid]
        while stack:
            neighbors = links[stack.pop()] - component
            component.update(neighbors)
            stack.extend(neighbors)
        visited.update(component)
        members = [products[key] for key in component]
        members.sort(key=lambda p: (p["created_at"], p["id"]))
        survivor, *duplicates = members
        reasons = {reason for p in members for reason in p["conflicts"]}
        ids = {p["id"] for p in members}
        categories_manual = set().union(*(manual_categories[p["id"]] for p in members))
        if ids & blocked_manual or len(categories_manual) > 1:
            reasons.add("manual_decision")
        profile = survivor["profile"]
        for other in duplicates:
            differences = identity_conflicts(profile, other["profile"])
            reasons.update(differences)
            if not differences:
                profile = accumulate_profile(profile, other["profile"])
        summary = {
            "key": identity_key(profile) or fingerprint(sorted(ids)),
            "survivor": survivor["id"],
            "duplicates": [p["id"] for p in duplicates],
            "proofs": sorted(set().union(*(proofs[member] for member in ids))),
            "categories": {p["id"]: p["category_id"] for p in members},
        }
        if reasons:
            blocked.append({**summary, "reasons": sorted(reasons)})
        else:
            categorized = [p for p in members if p["category_id"]]
            best = max(categorized, key=lambda p: p["category_weight"], default={})
            groups.append(
                {
                    **summary,
                    "profile": profile,
                    "category_id": next(iter(categories_manual), best.get("category_id")),
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

    def __init__(self, store, facts=()):
        """Использует SQL-репозиторий существующего каталога."""
        self.store = store
        self.facts = validate_references(facts)

    def preview(self) -> dict:
        """Создаёт план без записи в БД и без запросов классификации."""
        with self.store.readonly() as unit:
            plan = self.plan(unit)
        return {**plan, "preview_sha256": fingerprint(plan)}

    def plan(self, unit) -> dict:
        """Читает связанные факты и сведения о ручных решениях из одного снимка."""
        rows = unit.product_identity_rows()
        plan = plan_products(
            rows, unit.product_identity_manual(), self.facts, unit.approved_brand_aliases()
        )
        usable = []
        for group in plan["groups"]:
            if group["category_id"] and not unit.assignable_path(group["category_id"]):
                plan["blocked"].append({**group, "reasons": ["invalid_category"]})
            else:
                usable.append(group)
        plan["groups"] = usable
        plan["verified_facts_sha256"] = fingerprint(self.facts)
        plan["legacy_aliases"] = unit.product_merge_aliases()
        plan["attachments"] = source_attachments(plan, unit.unlinked_identity_rows(), self.facts)
        plan["integrity_before"] = unit.merge_integrity()
        plan["offer_routes_before"] = unit.merge_offer_routes({})
        plan["product_counts_before"] = unit.catalog_counts()["products"]
        plan["reference_sha256"] = {
            name: unit.merge_digest(name)
            for name in (
                "ingest_job_items",
                "product_identifiers",
                "product_match_decisions",
                "classification_decisions",
                "product_merge_redirects",
                "review_tasks",
                "taxonomy_gaps",
            )
        }
        plan["expected_deleted"] = sum(len(group["duplicates"]) for group in plan["groups"]) + len(
            plan["legacy_aliases"]
        )
        return plan

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
            mapping = {
                alias["old_product_id"]: alias["product_id"] for alias in plan["legacy_aliases"]
            }
            current = {
                pid: group["survivor"] for group in plan["groups"] for pid in group["duplicates"]
            }
            mapping = {old: current.get(target, target) for old, target in mapping.items()}
            mapping.update(current)
            expected_offer_routes = unit.merge_offer_routes(
                mapping, {match["offer_id"]: match["product_id"] for match in plan["attachments"]}
            )
            absorbed = {duplicate for group in plan["groups"] for duplicate in group["duplicates"]}
            enriched = 0
            for product in plan["products"]:
                if (
                    product["id"] not in absorbed
                    and not product["conflicts"]
                    and identity_retrieval_keys(product["profile"])
                ):
                    unit.remember_identity(product["id"], product["profile"])
                    enriched += 1
            transfers = {}
            unit.merge_candidate_routes(mapping)
            for alias in plan["legacy_aliases"]:
                moved = unit.merge_products(
                    {
                        "survivor": alias["product_id"],
                        "duplicates": [alias["old_product_id"]],
                        "legacy": True,
                    }
                )
                for name, count in moved.items():
                    transfers[name] = transfers.get(name, 0) + count
            for group in plan["groups"]:
                if group["category_id"] and not unit.assignable_path(group["category_id"]):
                    raise CatalogError(
                        "Категория группы больше не доступна. Объединение не применено."
                    )
                moved = unit.merge_products(group)
                for name, count in moved.items():
                    transfers[name] = transfers.get(name, 0) + count
            for match in plan["attachments"]:
                unit.attach_merged_source(match)
            after = unit.merge_integrity()
            physical = unit.catalog_counts()["products"]
            if (
                after != before
                or unit.merge_offer_routes({}) != expected_offer_routes
                or physical != plan["product_counts_before"] - plan["expected_deleted"]
            ):
                raise ConflictError("Проверка сохранности исходных данных и прогресса не прошла")
            result = {
                "preview_sha256": expected,
                "groups": len(plan["groups"]),
                "retired": len(absorbed),
                "deleted": plan["expected_deleted"],
                "legacy_deleted": len(plan["legacy_aliases"]),
                "merged_groups": [
                    {k: group[k] for k in ("survivor", "duplicates", "proofs")}
                    for group in plan["groups"]
                ],
                "enriched": enriched,
                "attached_sources": len(plan["attachments"]),
                "physical_products": physical,
                "blocked": len(plan["blocked"]),
                "moved": transfers,
                "integrity": after,
            }
            unit.save_command("operator_unverified", key, request, result)
        return result


def validate_references(facts) -> list[dict]:
    """Принимает ограниченный набор документов с проверяемыми полями вместо UUID."""
    if not isinstance(facts, (list, tuple)) or len(facts) > 64:
        raise CatalogError("Допустимо до 64 проверенных документов")
    for fact in facts:
        if not isinstance(fact, dict) or fact.keys() - {
            "claims",
            "document",
            "type_terms",
            "features",
        }:
            raise CatalogError("Неизвестное поле в документе подтверждения")
        claims = fact.get("claims")
        if (
            not isinstance(claims, dict)
            or not {"brand", "mpn"} <= claims.keys()
            or claims.keys() - {"brand", "mpn", "model", "manufacturer", "series", "mark"}
            or not all(isinstance(v, str) and v.strip() and len(v) <= 256 for v in claims.values())
            or not isinstance(fact.get("document"), str)
            or not source_locator(fact["document"])
        ):
            raise CatalogError("Документу нужны URL, бренд и подтверждённый артикул")
        terms = fact.get("type_terms", [])
        features = fact.get("features", {})
        if (
            not isinstance(terms, list)
            or not terms
            or not all(isinstance(term, str) and term.strip() for term in terms)
            or not isinstance(features, dict)
            or not all(
                isinstance(values, list) and values and all(isinstance(v, str) for v in values)
                for values in features.values()
            )
        ):
            raise CatalogError("Документу нужны признаки типа и корректные параметры исполнения")
    return list(facts)


def source_attachments(plan: dict, sources: list[dict], references: list[dict]) -> list[dict]:
    """Проверяет отдельные завершённые позиции без связки Product по тому же индексу."""
    merged = {pid for group in plan["groups"] for pid in [group["survivor"], *group["duplicates"]]}
    targets = {
        p["id"]: (p["profile"], p["category_id"])
        for p in plan["products"]
        if p["id"] not in merged and not p["conflicts"]
    }
    targets.update({g["survivor"]: (g["profile"], g["category_id"]) for g in plan["groups"]})
    index = defaultdict(list)
    for pid, (profile, _) in targets.items():
        for key in identity_retrieval_keys(profile):
            if len(index[key]) <= IDENTITY_LIMIT:
                index[key].append(pid)
    output = []
    for row in sources:
        answer = row["answer"]
        if answer.get("record_kind") in {"category_listing", "other"}:
            continue
        profile = identity_profile({**row["fields"], "origin_url": row["origin_url"]}, answer)
        for reference in references:
            profile = with_verified_reference(profile, reference)
        if answer.get("status") != "recognized" and not any(
            claim["evidence"] == "verified_document" for claim in profile["claims"].values()
        ):
            continue
        candidates = sorted({pid for key in identity_retrieval_keys(profile) for pid in index[key]})
        result = decide_identity(
            profile,
            [(k, targets[k][0]) for k in candidates[:IDENTITY_LIMIT]],
            overflow=len(candidates) > IDENTITY_LIMIT,
        )
        if target := result["product_id"]:
            if row["category_id"] and str(row["category_id"]) != targets[target][1]:
                continue
            details = {
                key: row[key]
                for key in (
                    "id",
                    "raw_record_id",
                    "normalized_record_id",
                    "status",
                    "offer_id",
                    "processing_version",
                )
            }
            output.append(
                {
                    **json_value(details),
                    "product_id": target,
                    "proof": next(
                        r["proof"] for r in result["candidates"] if r["product_id"] == target
                    ),
                }
            )
    offer_counts = Counter(r["offer_id"] for r in output)
    return [r for r in output if offer_counts[r["offer_id"]] == 1]
