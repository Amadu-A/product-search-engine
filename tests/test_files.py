# tests/test_files.py
"""Контракт JSON/HTML, checksum исходных байтов и защита пути bundle."""

import json
from pathlib import Path

import pytest

from app.domain.taxonomy import TaxonomyError, snapshot_hash
from app.infrastructure.files import (
    html_nodes,
    load_snapshot,
    normalize_file,
    read_limited,
    write_json,
)


def test_json_roundtrip(snapshot, tmp_path):
    """Плоский JSON сохраняет родительство, пути, UUID и лицензионные уведомления."""
    restored = load_snapshot(tmp_path / "bundle/categories.json", 8388608)
    assert restored == snapshot
    assert restored.source.license_notice


def test_html_matches_json(snapshot, tmp_path):
    """Два собственных формата приводятся к одной структуре, включая устойчивые UUID."""
    metadata = tmp_path / "metadata.json"
    write_json(metadata, {"source": snapshot.source.model_dump(), "complete": True})
    parsed = normalize_file(Path("tests/fixtures/tree.html"), tmp_path / "html", 8388608, metadata)
    assert [n.imported_values() for n in parsed.nodes] == [
        n.imported_values() for n in snapshot.nodes
    ]


@pytest.mark.parametrize(
    "html",
    [
        b'<main><a href="/catalog/42/">Link</a></main>',
        b'<ul data-taxonomy><li data-kind="product"><span data-name>Item</span></li></ul>',
        b"<ul data-taxonomy><li><a>A</a><a>B</a></li></ul>",
    ],
)
def test_unknown_or_product_html_is_rejected(html):
    """Парсер не угадывает родительство по всем ссылкам и не принимает товары."""
    with pytest.raises(TaxonomyError):
        html_nodes(html)


def test_raw_tampering_is_rejected(snapshot, tmp_path):
    """Правка исходных байтов после экспорта ломает checksum до открытия БД."""
    (tmp_path / "bundle" / snapshot.files[0].file).write_bytes(b"changed")
    with pytest.raises(TaxonomyError, match="исходного"):
        load_snapshot(tmp_path / "bundle/categories.json", 8388608)


def test_json_tampering_is_rejected(snapshot, tmp_path):
    """Правка нормализованного дерева не скрывается прежним SHA."""
    path = tmp_path / "bundle/categories.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["nodes"][0]["name"] = "Изменено"
    write_json(path, data)
    with pytest.raises(TaxonomyError, match="categories.json"):
        load_snapshot(path, 8388608)


def test_path_traversal_rejected(snapshot, tmp_path):
    """Даже подписанный самим оператором JSON не читает файлы вне bundle."""
    changed = snapshot.model_copy(
        update={"files": (snapshot.files[0].model_copy(update={"file": "../../private.env"}),)}
    )
    path = tmp_path / "bundle/categories.json"
    write_json(path, changed.model_dump(mode="json"))
    path.with_suffix(".sha256").write_text(snapshot_hash(changed), encoding="ascii")
    with pytest.raises(TaxonomyError, match="вне каталога"):
        load_snapshot(path, 8388608)


def test_size_limit(tmp_path):
    """Переполнение отсеивается до передачи parser."""
    path = tmp_path / "large.json"
    path.write_bytes(b"x" * 10)
    with pytest.raises(TaxonomyError, match="размера"):
        read_limited(path, 5)
