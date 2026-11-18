# tests/conftest.py
"""Фикстуры собственных деревьев и отдельной PostgreSQL schema; production не очищается."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.domain.taxonomy import Review, Sample, Snapshot, snapshot_hash
from app.infrastructure.files import normalize_file
from app.settings import Settings


@pytest.fixture
def snapshot(tmp_path: Path) -> Snapshot:
    """Нормализует собственный классификатор, сохраняя все артефакты в pytest basetemp."""
    return normalize_file(Path("tests/fixtures/tree.json"), tmp_path / "bundle", 8388608)


def approved_review(snapshot: Snapshot) -> Review:
    """Создаёт подтверждение только синтетического дерева для автоматических тестов."""
    return Review(
        snapshot_sha256=snapshot_hash(snapshot),
        reviewed_by="Автоматический тест",
        reviewed_at=datetime.now(UTC),
        reference="Сверка с собственными tests/fixtures/tree.json",
        complete=True,
        samples=tuple(
            Sample(
                id=n.id,
                name=n.name,
                path=n.path,
                source_url=n.source_url,
            )
            for n in snapshot.nodes
        ),
    )


@pytest.fixture
def review(snapshot: Snapshot) -> Review:
    """Подтверждает выборку всех узлов синтетического снимка."""
    return approved_review(snapshot)


@pytest.fixture
def db_engine():
    """Применяет настоящую миграцию в уникальной schema только БД taxonomy_test."""
    url_string = Settings().test_database_url.get_secret_value()
    if not url_string:
        pytest.skip("TEST_DATABASE_URL не задан; реальная PostgreSQL проверяется отдельно")
    url = make_url(url_string)
    if url.database != "taxonomy_test":
        pytest.fail("Integration tests разрешены исключительно в БД taxonomy_test")
    admin = sa.create_engine(url, hide_parameters=True)
    schema = "taxonomy_test_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = sa.create_engine(
        url,
        hide_parameters=True,
        connect_args={
            "options": f"-c search_path={schema} -c statement_timeout=30000 -c lock_timeout=5000"
        },
    )
    try:
        config = Config("alembic.ini")
        config.attributes["engine"] = engine
        command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        assert schema.startswith("taxonomy_test_") and len(schema) == 46
        with admin.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
