# tests/test_import_use_case.py
"""Unit use-case: проверки применимости выполняются до доступа к БД."""

import pytest

from app.application.import_tree import ImportTree
from app.domain.taxonomy import TaxonomyError


def test_apply_requires_review_before_uow(snapshot):
    """Даже разрешённая синтетика не импортируется без независимого review."""
    with pytest.raises(TaxonomyError, match="review"):
        ImportTree(None, 20000, 32).execute(snapshot, apply=True)


def test_invalid_tree_never_opens_uow(snapshot):
    """Неполный снимок не обращается к инфраструктуре даже для dry-run."""
    with pytest.raises(TaxonomyError, match="неполное"):
        ImportTree(None, 20000, 32).execute(snapshot.model_copy(update={"complete": False}))
