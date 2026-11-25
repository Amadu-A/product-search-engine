# tests/test_category_index_regression.py
"""Регрессии реального Qdrant: alias, перенос, удаление и конкурентный SQL snapshot."""

import pytest

from tests.test_catalog_pipeline import catalog as catalog

pytestmark = pytest.mark.integration


def test_alias_move_delete_and_dimension_switch(catalog):
    """Подтверждённые aliases, SQL move/delete и новая размерность отражаются без смены UUID."""
    first = catalog.index.sync()
    rows = catalog.store.categories()
    leaf = next(r for r in rows if r["name"] == "Цемент")
    root = next(r for r in rows if r["name"] == "Инженерные системы")
    old_hash = catalog.index.scroll(first["collection_name"])[str(leaf["id"])]["document_sha256"]
    with catalog.store.transaction() as unit:
        alias = unit.add(
            "category_aliases",
            category_id=leaf["id"],
            alias="Синтетический синоним",
            normalized_alias="синтетическийсиноним",
            status="approved",
            approved_by="test",
        )
        unit.update("categories", leaf["id"], parent_id=root["id"])
    moved = catalog.index.sync()
    point = catalog.index.scroll(moved["collection_name"])[str(leaf["id"])]
    assert point["path_names"] == ["Инженерные системы", "Цемент"]
    assert point["document_sha256"] != old_hash
    with catalog.store.transaction() as unit:
        unit.update("category_aliases", alias["id"], status="rejected")
    rejected = catalog.index.sync()
    assert (
        catalog.index.scroll(rejected["collection_name"])[str(leaf["id"])]["document_sha256"]
        != point["document_sha256"]
    )
    catalog.inference.dimensions = 32
    changed = catalog.index.sync()
    assert changed["collection_name"] != first["collection_name"]
    assert changed["embedding_dimensions"] == 32
    assert catalog.index.request("GET", "/collections/" + first["collection_name"])
    deletable = next(r for r in catalog.store.categories() if r["name"] == "Штукатурка")
    with catalog.store.transaction() as unit:
        table = unit.table("categories")
        unit.connection.execute(table.delete().where(table.c.id == deletable["id"]))
    deleted = catalog.index.sync()
    assert deleted["points"] == 7
    assert str(deletable["id"]) not in catalog.index.scroll(deleted["collection_name"])
    catalog.index.request("DELETE", "/collections/" + first["collection_name"])


def test_change_during_embedding_is_pending_until_next_snapshot(catalog, monkeypatch):
    """Событие после snapshot revision не ACK-ается устаревшим worker build."""
    row = catalog.store.categories()[0]
    original = catalog.inference.embed
    called = False

    def concurrent_change(documents):
        """Коммитит новое имя после чтения SQL snapshot, до записи Qdrant."""
        nonlocal called
        if not called:
            called = True
            with catalog.store.transaction() as unit:
                unit.update("categories", row["id"], name="Конкурентное новое имя")
        return original(documents)

    monkeypatch.setattr(catalog.inference, "embed", concurrent_change)
    result = catalog.index.sync()
    assert result["status"] == "stale" and result["pending_events"] == 1
    result = catalog.index.sync()
    assert result["status"] == "ready" and result["pending_events"] == 0
    point = catalog.index.scroll(result["collection_name"])[str(row["id"])]
    assert point["name"] == "Конкурентное новое имя"
