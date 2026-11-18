# app/application/ports.py
"""Минимальные порты атомарного импорта и SQL-отчётности без зависимости от драйвера."""

from typing import Protocol

from app.domain.planning import ImportPlan
from app.domain.taxonomy import Permission, Review, Snapshot


class ImportUnitOfWork(Protocol):
    """Контекст одной транзакции, откатываемой без явного commit."""

    def __enter__(self) -> "ImportUnitOfWork":
        """Открывает изолированную транзакцию и блокировку импортёра."""
        ...

    def __exit__(self, *args: object) -> None:
        """Откатывает незавершённую транзакцию и освобождает соединение."""
        ...

    def list_categories(self) -> list[dict]:
        """Возвращает значения и last-import baseline всех категорий."""
        ...

    def persist(
        self,
        snapshot: Snapshot,
        review: Review,
        permission: Permission | None,
        plan: ImportPlan,
        commit_sha: str,
    ) -> str:
        """Записывает изменения, полный снимок и аудит в текущую транзакцию."""
        ...

    def commit(self) -> None:
        """Фиксирует только успешно завершённую application-операцию."""
        ...


class ReportRepository(Protocol):
    """Порт согласованного отчёта из фактического состояния PostgreSQL."""

    def inspect(self) -> dict:
        """Возвращает SQL-метрики дерева и аудит выполненных импортов."""
        ...
