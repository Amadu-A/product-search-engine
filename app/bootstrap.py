# app/bootstrap.py
"""Composition root: единственное место сборки concrete DB/HTTP adapters для CLI."""

from app.application.import_tree import ImportTree
from app.application.report import BuildReport
from app.infrastructure.database import SqlImportUnitOfWork, create_db_engine
from app.infrastructure.reporting import SqlReportRepository
from app.settings import Settings


def build_import(settings: Settings, *, apply: bool) -> ImportTree:
    """Собирает application-операцию с read-only UoW для предварительного просмотра."""
    return ImportTree(
        SqlImportUnitOfWork(create_db_engine(settings), readonly=not apply),
        settings.taxonomy_max_nodes,
        settings.taxonomy_max_depth,
    )


def build_report(settings: Settings) -> BuildReport:
    """Собирает отчёт с отдельным SQL read-only adapter."""
    return BuildReport(SqlReportRepository(create_db_engine(settings)))
