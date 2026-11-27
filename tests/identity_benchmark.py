# tests/identity_benchmark.py
"""Opt-in replay каталога из сохранённых answer_1 с отдельной оценкой снимка."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from itertools import combinations, islice
from pathlib import Path
from time import perf_counter
from types import ModuleType

from app.domain import product_identity as identity


def read_inputs(folder: Path) -> tuple[dict, dict]:
    """Проверяет, что разметка относится к неизменённой выгрузке."""
    source = (folder / "catalog_identity_export.json").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    metadata = {"source_sha256": digest}
    files = {
        "groups": "identity_ground_truth.json",
        "pairs": "identity_negative_pairs.json",
        "cases": "identity_uncertain.json",
    }
    for field, filename in files.items():
        content = (folder / filename).read_bytes()
        data = json.loads(content)
        if data["source_sha256"] != digest:
            raise ValueError(f"SHA-256 выгрузки не соответствует {filename}")
        metadata[field] = data[field]
        metadata[filename] = hashlib.sha256(content).hexdigest()
    return json.loads(source), metadata


def replay(source: dict, policy, facts: list) -> tuple[dict, dict, dict, dict]:
    """Повторяет подбор по исходникам. Разметка передаётся только в оценку."""
    normalized = {row["id"]: row["attributes"]["fields"] for row in source["normalized_records"]}
    answers = {
        row["normalized_record_id"]: row["answer_json"] or {}
        for row in sorted(source["understandings"], key=lambda r: (r["created_at"], r["id"]))
        if row["status"] in {"recognized", "insufficient_evidence"}
    }
    raw = {row["id"]: row for row in source["raw_records"]}
    catalog, associations, candidates, decisions = {}, {}, {}, {}
    index = defaultdict(dict)
    stats = Counter()
    for item in sorted(source["items"], key=lambda r: (r["created_at"], r["id"])):
        raw_id = item["raw_record_id"]
        norm_id = item["normalized_record_id"]
        fields = {**normalized.get(norm_id, {}), "origin_url": raw[raw_id].get("origin_url")}
        answer = answers.get(norm_id, {})
        recognized = (
            answer.get("status") == "recognized"
            and answer.get("record_kind") == "product_candidate"
        )
        if not recognized:
            stats["without_recognized_answer"] += 1
        profile = policy.identity_profile(fields, answer)
        for reference in facts:
            profile = policy.with_verified_reference(profile, reference)
        verified = any(v["evidence"] == "verified_document" for v in profile["claims"].values())
        if not recognized and (
            not verified or answer.get("record_kind") in {"other", "category_listing"}
        ):
            decisions[raw_id] = {"decision": "unresolved", "reason": "insufficient_answer_1"}
            continue
        stats["verified_rows"] += verified
        key = policy.identity_key(profile)
        keys = [key] if key else []
        lookup = getattr(policy, "identity_retrieval_keys", None)
        if lookup:
            keys = lookup(profile)
        found = {}
        for key in keys:
            found.update(islice(index[key].items(), 33))
        overflow = len(found) > 32
        found = dict(list(found.items())[:32])
        candidates[raw_id] = list(found)
        stats["candidate_checks"] += len(found)
        decide = getattr(policy, "decide_identity", None)
        if decide:
            decision = decide(profile, [(pid, catalog[pid]) for pid in found], overflow=overflow)
            target = decision["product_id"]
            stats[decision["decision"]] += 1
        else:
            compatible = [p for p in found if not policy.identity_conflicts(catalog[p], profile)]
            target = compatible[0] if len(compatible) == 1 and not overflow else None
            decision = {"decision": "same" if target else "different", "product_id": target}
            stats[decision["decision"]] += 1
        decisions[raw_id] = decision
        if target:
            profile = policy.accumulate_profile(catalog[target], profile)
        else:
            target = raw_id
        catalog[target] = profile
        associations[raw_id] = target
        keys = lookup(profile) if lookup else [key for key in [policy.identity_key(profile)] if key]
        for key in keys:
            index[key][target] = None
    return (
        associations,
        candidates,
        decisions,
        {
            **stats,
            "product_count": len(catalog),
            "vlm_requests": [0, 0, 0, 0],
        },
    )


def evaluate(labels: dict, linked: dict, retrieved: dict | None = None, decisions=None) -> dict:
    """Считает совпадения отдельно для подтверждённых, отрицательных и спорных пар."""
    stats = defaultdict(Counter)
    detail, labeled_pairs = {}, set()
    for field, id_field in (("groups", "group_id"), ("pairs", "pair_id"), ("cases", "case_id")):
        for row in labels[field]:
            members = [member["raw_record_id"] for member in row["members"]]
            category = row.get("evidence_grade", field)
            matching = found = 0
            for a, b in combinations(members, 2):
                labeled_pairs.add(tuple(sorted((a, b))))
                same = linked.get(a) is not None and linked.get(a) == linked.get(b)
                matching += same
                if retrieved is not None:
                    found += linked.get(a) in retrieved.get(b, []) or linked.get(
                        b
                    ) in retrieved.get(a, [])
                stats[category]["pairs"] += 1
            stats[category]["matched_pairs"] += matching
            stats[category]["cases"] += 1
            stats[category]["cases_with_matches"] += matching > 0
            stats[category]["all_members_linked"] += (
                len(members) > 1 and matching == len(members) * (len(members) - 1) // 2
            )
            if retrieved is not None:
                stats[category]["retrieved_pairs"] += found
            detail[row[id_field]] = {
                "matched_pairs": matching,
                "retrieved_pairs": found,
                "decisions": [(decisions or {}).get(member) for member in members],
            }
    positives = [stats[k] for k in ("externally_corroborated", "source_consistent")]
    tp = sum(group["matched_pairs"] for group in positives)
    positive_pairs = sum(group["pairs"] for group in positives)
    fp = stats["pairs"]["matched_pairs"]
    fn = positive_pairs - tp
    sizes = Counter(pid for pid in linked.values() if pid)
    unlabeled = sum(n * (n - 1) // 2 for n in sizes.values())
    unlabeled -= sum(
        linked.get(a) is not None and linked.get(a) == linked.get(b) for a, b in labeled_pairs
    )
    return {
        "summary": stats,
        "cases": detail,
        "metrics": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / positive_pairs if positive_pairs else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            "unlabeled_matched_pairs": unlabeled,
            "uncertain_linked_cases": stats["cases"]["cases_with_matches"],
        },
    }


def main() -> None:
    """Создаёт отдельный JSON-отчёт в .tmp/reports по явно указанной выгрузке."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("examples/identity_benchmark"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy-file", type=Path, help="Сохранённый модуль исходной политики")
    parser.add_argument("--facts", type=Path, help="Отдельный список проверенных документов")
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to(Path(".tmp/reports").resolve()):
        parser.error("Отчёт должен находиться в .tmp/reports")
    source, labels = read_inputs(args.dataset)
    policy = identity
    if args.policy_file:
        policy = ModuleType("benchmark_reference_policy")
        exec(compile(args.policy_file.read_bytes(), str(args.policy_file), "exec"), policy.__dict__)
    facts = json.loads(args.facts.read_bytes()) if args.facts else []
    started = perf_counter()
    linked, retrieved, decisions, stats = replay(source, policy, facts)
    report = {
        "inputs": {k: v for k, v in labels.items() if not isinstance(v, list)},
        "snapshot": evaluate(
            labels, {i["raw_record_id"]: i["product_id"] for i in source["items"]}
        ),
        "replay": evaluate(labels, linked, retrieved, decisions),
        "work": {**stats, "seconds": round(perf_counter() - started, 3)},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"replay": report["replay"]["summary"], "work": report["work"]}))


if __name__ == "__main__":
    main()
