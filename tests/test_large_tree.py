# tests/test_large_tree.py
"""Большая собственная матрица: 5055 категорий без копирования внешнего классификатора."""

from app.domain.taxonomy import validate_tree
from app.infrastructure.files import normalize_file, write_json


def test_large_synthetic_tree(snapshot, tmp_path):
    """Проверяет масштаб, полные пути и стабильные ключи на дереве 5 × 10 × 100."""
    roots = [
        {
            "external_id": f"r-{root}",
            "name": f"Корень {root}",
            "children": [
                {
                    "external_id": f"b-{root}-{branch}",
                    "name": f"Ветвь {branch}",
                    "children": [
                        {"external_id": f"l-{root}-{branch}-{leaf}", "name": f"Лист {leaf}"}
                        for leaf in range(100)
                    ],
                }
                for branch in range(10)
            ],
        }
        for root in range(5)
    ]
    path = tmp_path / "large.json"
    write_json(path, {"source": snapshot.source.model_dump(), "complete": True, "nodes": roots})
    result = normalize_file(path, tmp_path / "large-bundle", 8388608)
    metrics = validate_tree(result)
    assert metrics["total"] == 5055 and metrics["roots"] == 5
    assert metrics["leaves"] == 5000 and metrics["levels"] == {1: 5, 2: 50, 3: 5000}
