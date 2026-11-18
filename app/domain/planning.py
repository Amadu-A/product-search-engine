# app/domain/planning.py
"""Чистый план импорта: сохранение custom, исчезнувших узлов и ручных правок."""

from dataclasses import dataclass, field

from app.domain.taxonomy import Snapshot, TaxonomyError


@dataclass(frozen=True)
class ImportPlan:
    """Изменения одной транзакции и счётчики, вычисленные по реальным данным."""

    added: tuple[dict, ...] = ()
    updated: tuple[dict, ...] = ()
    skipped: int = 0
    retained: int = 0
    conflicts: tuple[str, ...] = ()
    before: int = 0
    metrics: dict = field(default_factory=dict)

    def counts(self) -> dict:
        """Возвращает компактный результат для CLI и аудита без содержимого категорий."""
        return {
            "before": self.before,
            "after": self.before + len(self.added),
            "added": len(self.added),
            "updated": len(self.updated),
            "skipped": self.skipped,
            "retained": self.retained,
            "conflicts": list(self.conflicts),
            "metrics": self.metrics,
        }


def plan_import(snapshot: Snapshot, existing: list[dict], metrics: dict) -> ImportPlan:
    """Сравнивает текущие значения с последним импортом; конфликты блокируют весь apply."""
    by_key = {
        (row["source"], row["external_id"]): row
        for row in existing
        if row["source"] == snapshot.source.key
    }
    ids = {str(row["id"]): row for row in existing}
    added, updated, conflicts = [], [], []
    skipped = 0
    incoming_keys = set()
    for node in sorted(snapshot.nodes, key=lambda n: n.level):
        values = node.imported_values()
        key = (snapshot.source.key, values["external_id"])
        incoming_keys.add(key)
        row = by_key.get(key)
        if row is None:
            if values["id"] in ids:
                conflicts.append(values["id"])
            else:
                added.append(values)
            continue
        current = {name: row[name] for name in values}
        if current != row.get("imported_values") or current["id"] != values["id"]:
            conflicts.append(values["id"])
        elif current == values:
            skipped += 1
        else:
            updated.append(values)
    return ImportPlan(
        tuple(added),
        tuple(updated),
        skipped,
        len(set(by_key) - incoming_keys),
        tuple(conflicts),
        len(existing),
        metrics,
    )


def require_no_conflicts(plan: ImportPlan) -> None:
    """Требует явного решения оператора при ручном редактировании imported-полей."""
    if plan.conflicts:
        raise TaxonomyError("Ручные изменения: импорт остановлен; UUID=" + ",".join(plan.conflicts))
