# app/application/catalog_ports.py
"""Порты общего импорта: SQL, форматные адаптеры, inference и поисковая проекция."""

from pathlib import Path
from typing import Any, Protocol

from app.domain.catalog import ParsedCatalog


class FileParser(Protocol):
    """Читает ограниченный локальный файл без побочных сетевых запросов."""

    def parse(self, path: Path, filename: str, options: dict) -> ParsedCatalog:
        """Возвращает raw и mapping отдельно."""
        ...


class CatalogStore(Protocol):
    """Транзакционный адаптер jobs, истории решений и номенклатуры."""

    def transaction(self) -> Any:
        """Открывает атомарный UoW, откатываемый при исключении."""
        ...

    def categories(self) -> list[dict]:
        """Читает актуальное дерево из SQL."""
        ...

    def index_snapshot(self) -> tuple[int, list[dict]]:
        """Читает revision и дерево в одном согласованном снимке."""
        ...

    def record_lock(self, record_id) -> Any:
        """Сериализует обработку общей normalized записи."""
        ...


class Understanding(Protocol):
    """Строгий двухшаговый inference с диагностируемой недоступностью."""

    mode: str
    model_revision: str
    embedding_revision: str

    def budget(self) -> Any:
        """Ограничивает и считает запросы VLM одной позиции."""
        ...

    def provenance(self, step: int, values: dict, resolve: bool = True) -> dict:
        """Формирует проверяемый ключ cache."""
        ...

    def answer(self, step: int, values: dict) -> dict:
        """Возвращает schema-validated ответ и provenance."""
        ...


class CategorySearch(Protocol):
    """SQL остаётся источником истины; vector search возвращает только кандидатов."""

    def candidates(self, understanding: dict) -> tuple[list[dict], list[str]]:
        """Объединяет dense и lexical, затем повторно проверяет SQL."""
        ...

    def status(self) -> dict:
        """Возвращает durable статус проекции."""
        ...


class JobQueue(Protocol):
    """Доставляет UUID durable задания без передачи raw по брокеру."""

    def publish(self, job_id: str) -> None:
        """Публикует подтверждённое задание либо сообщает недоступность."""
        ...
