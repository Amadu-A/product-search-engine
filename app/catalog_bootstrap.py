# app/catalog_bootstrap.py
"""Composition root этапа 02 без создания параллельных сервисов inference."""

from app.application.catalog_import import CatalogImport
from app.infrastructure.catalog_formats import CatalogFileParser
from app.infrastructure.catalog_store import CatalogStore
from app.infrastructure.category_index import CategoryIndex
from app.infrastructure.database import create_db_engine
from app.infrastructure.inference import SharedInference
from app.infrastructure.job_queue import JobQueue
from app.settings import Settings


def build_catalog(settings: Settings | None = None, engine=None) -> CatalogImport:
    """Собирает те же use-case для CLI, HTTP и worker; engine заменяется только тестом."""
    settings = settings or Settings()
    store = CatalogStore(engine or create_db_engine(settings))
    inference = SharedInference(settings)
    return CatalogImport(
        store,
        CatalogFileParser(settings),
        inference,
        CategoryIndex(store, inference, settings),
        JobQueue(settings),
        settings,
    )
