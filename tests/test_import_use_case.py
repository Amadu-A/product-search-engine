# tests/test_import_use_case.py
"""Unit use-case: только технические дефекты блокируют локальный импорт до обращения к БД."""

import pytest

from app.application.import_tree import ImportTree
from app.domain.taxonomy import TaxonomyError


class MemoryUow:
    """Минимальный порт для проверки orchestration без PostgreSQL."""

    def __init__(self):
        """Сохраняет признак commit и реально переданные сведения аудита."""
        self.committed = False
        self.saved = None

    def __enter__(self):
        """Открывает тестовую транзакцию без внешних side effects."""
        return self

    def __exit__(self, *args):
        """Закрывает тестовый контекст без скрытого commit."""

    def list_categories(self):
        """Предоставляет пустую БД для первого импорта."""
        return []

    def persist(self, snapshot, review, permission, plan, commit_sha):
        """Запоминает аудит без создания фиктивного разрешения или review."""
        self.saved = (snapshot, review, permission)
        return "test-run"

    def commit(self):
        """Отмечает явную фиксацию use-case."""
        self.committed = True


def test_apply_partial_without_permission_or_review(partial_snapshot):
    """Неполный локальный Petrovich JSON с UNKNOWN/unverified импортируется технически."""
    uow = MemoryUow()
    result = ImportTree(uow, 20000, 32).execute(partial_snapshot, apply=True)
    assert result["added"] == 8 and result["metrics"]["complete"] is False
    assert uow.committed and uow.saved[1:] == (None, None)
    assert result["metrics"]["source_information"]["usage_status"] == "unverified"


def test_invalid_tree_never_opens_uow(snapshot):
    """Пустое дерево проверяется до доступа к БД независимо от наличия разрешений."""
    with pytest.raises(TaxonomyError, match="Пустое"):
        ImportTree(None, 20000, 32).execute(snapshot.model_copy(update={"nodes": ()}), apply=True)
