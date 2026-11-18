# migrations/env.py
"""Применяет только проектные миграции, используя тот же layered Settings, что CLI."""

from alembic import context

from app.infrastructure.database import create_db_engine, metadata
from app.settings import Settings


def run_migrations() -> None:
    """Выполняет миграции онлайн без печати URL и пароля подключения."""
    engine = context.config.attributes.get("engine") or create_db_engine(Settings())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
