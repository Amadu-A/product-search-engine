# tests/test_taxonomy.py
"""Unit/regression проверки дерева, идентификаторов, прав и review конкретного SHA."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.taxonomy import (
    Permission,
    TaxonomyError,
    category_id,
    stable_key,
    validate_review,
    validate_tree,
)


def test_metrics_and_same_names(snapshot):
    """Повтор названий у разных родителей допустим, точные метрики фиксированы."""
    assert validate_tree(snapshot) == {
        "total": 8,
        "roots": 2,
        "leaves": 5,
        "max_depth": 3,
        "levels": {1: 2, 2: 4, 3: 2},
        "duplicates": 0,
        "cycles": 0,
        "orphans": 0,
        "errors": 0,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "orphan",
        "cycle",
        "name",
        "level",
        "path",
        "incomplete",
        "source",
        "unstable_id",
        "order",
        "raw_hash",
        "date",
        "product_url",
        "external_id",
    ],
)
def test_rejects_corrupted_tree(snapshot, mutation):
    """Любое структурное повреждение блокируется до транзакции."""
    nodes = list(snapshot.nodes)
    changes = {
        "orphan": {"parent_id": uuid4()},
        "cycle": {"parent_id": nodes[2].id},
        "name": {"name": " "},
        "level": {"level": 9},
        "path": {"path": ("Чужой путь",)},
        "source": {"source": "custom"},
        "unstable_id": {"id": uuid4()},
        "date": {"extracted_at": datetime.now()},
        "product_url": {"source_url": "http://127.0.0.1/product/42"},
        "external_id": {"external_id": "  "},
    }
    if mutation in changes:
        nodes[0] = nodes[0].model_copy(update=changes[mutation])
    elif mutation == "duplicate":
        nodes.append(nodes[0])
    elif mutation == "order":
        nodes[-1] = nodes[-1].model_copy(update={"sort_order": 0})
    update = {"nodes": tuple(nodes)}
    if mutation == "incomplete":
        update["complete"] = False
    if mutation == "raw_hash":
        update["raw_sha256"] = "0" * 64
    with pytest.raises(TaxonomyError):
        validate_tree(snapshot.model_copy(update=update))


def test_depth_and_count_limits(snapshot):
    """Слишком большое или глубокое дерево отклоняется явными лимитами."""
    with pytest.raises(TaxonomyError):
        validate_tree(snapshot, max_nodes=2)
    with pytest.raises(TaxonomyError):
        validate_tree(snapshot, max_depth=2)


def test_stable_url_fallback_and_source_namespace():
    """URL fallback воспроизводим; источники не делят UUID одного внешнего ID."""
    url = "https://example.org/categories/pipe/"
    assert stable_key(None, url) == "url:" + url
    assert category_id("first", None, url) == category_id("first", None, url)
    assert category_id("first", "42", url) != category_id("second", "42", url)


def test_review_is_bound_to_exact_snapshot(snapshot, review):
    """Review чужого SHA, неполной выборки или неверного названия не применяется."""
    validate_review(snapshot, review)
    for update in (
        {"snapshot_sha256": "0" * 64},
        {"samples": review.samples[:1]},
        {"samples": (review.samples[0].model_copy(update={"name": "Иное"}),)},
        {"reviewed_at": datetime.now(UTC) + timedelta(days=1)},
    ):
        with pytest.raises(TaxonomyError):
            validate_review(snapshot, review.model_copy(update=update))


def test_permission_is_not_inferred(snapshot):
    """Синтетика разрешена для теста, Petrovich и NOASSERTION требуют реального основания."""
    snapshot.source.require_reuse(None)
    with pytest.raises(TaxonomyError):
        snapshot.source.model_copy(update={"key": "petrovich"}).require_reuse(None)
    with pytest.raises(TaxonomyError):
        snapshot.source.model_copy(update={"license": "NOASSERTION"}).require_reuse(None)
    permission = Permission(
        source="petrovich",
        approved_by="Тестовый оператор",
        approved_at=datetime.now(UTC),
        reference="Фиктивное основание исключительно unit-test",
        allow_fetch=False,
        allow_reuse=False,
    )
    with pytest.raises(TaxonomyError):
        permission.require("fetch")
