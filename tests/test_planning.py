# tests/test_planning.py
"""Unit-матрица идемпотентности и защиты исходных ручных данных."""

import pytest

from app.domain.planning import plan_import, require_no_conflicts
from app.domain.taxonomy import TaxonomyError, validate_tree


def rows_of(snapshot):
    """Строит last-import baseline без зависимости чистых тестов от PostgreSQL."""
    return [{**n.imported_values(), "imported_values": n.imported_values()} for n in snapshot.nodes]


def test_initial_and_repeat(snapshot):
    """Первая загрузка добавляет восемь, повтор не изменяет ни одного узла."""
    first = plan_import(snapshot, [], validate_tree(snapshot))
    assert len(first.added) == 8
    repeated = plan_import(snapshot, rows_of(snapshot), validate_tree(snapshot))
    assert repeated.skipped == 8
    assert not repeated.added and not repeated.updated and not repeated.conflicts


def test_manual_change_blocks_entire_apply(snapshot):
    """Ручное изменение имени не перезаписывается даже прежним снимком."""
    rows = rows_of(snapshot)
    rows[0]["name"] = "Ручное название"
    plan = plan_import(snapshot, rows, {})
    assert plan.conflicts == (str(snapshot.nodes[0].id),)
    with pytest.raises(TaxonomyError):
        require_no_conflicts(plan)


def test_missing_categories_and_custom_are_retained(snapshot):
    """Отсутствующие в следующем снимке и пользовательские строки не удаляются."""
    rows = rows_of(snapshot)
    rows.append({"id": "custom-id", "source": "custom", "external_id": None})
    reduced = snapshot.model_copy(update={"nodes": snapshot.nodes[:1]})
    plan = plan_import(reduced, rows, {})
    assert plan.retained == 7
    assert plan.before == 9 and plan.counts()["after"] == 9


def test_source_update_and_foreign_id_conflict(snapshot):
    """Изменение источника планируется, а несовпадение внутреннего ID блокируется."""
    rows = rows_of(snapshot)
    changed = snapshot.model_copy(
        update={
            "nodes": (
                snapshot.nodes[0].model_copy(update={"name": "Новое название"}),
                *snapshot.nodes[1:],
            )
        }
    )
    assert len(plan_import(changed, rows, {}).updated) == 1
    rows[0]["id"] = "manually-changed-id"
    rows[0]["imported_values"] = {**rows[0]["imported_values"], "id": rows[0]["id"]}
    assert plan_import(snapshot, rows, {}).conflicts
