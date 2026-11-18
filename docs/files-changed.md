<!-- docs/files-changed.md -->
# Файлы этапа 1

Изменён `.gitignore`; остальные перечисленные файлы созданы в рамках этапа.
Исходные пользовательские `AGENTS.md` и `renders/render.png` присутствовали до
работы и не изменялись.

Контракты: вложенный JSON/семантический HTML, плоский snapshot с checksum,
review точного SHA, provenance/license, read-only preview и отдельный явный apply.
Миграция `0001_taxonomy` создаёт `categories`, `taxonomy_snapshots`,
`taxonomy_import_runs`. Persistence: PostgreSQL. Transaction boundary:
`ImportTree` через `SqlImportUnitOfWork`; сборка зависимостей — `app/bootstrap.py`
и CLI entrypoint. Shared-сервисы не используются. У ключевых операций импорта
и отчётности единый timing-декоратор; Docker logs ограничены ротацией.
Каталог переменных — `.env.example`; приватный `.env` up.sh не создаёт и не перезаписывает.

Полный перечень изменённых/созданных файлов:

- `.dockerignore`
- `.env.example`
- `.gitattributes`
- `.github/workflows/quality.yaml`
- `.gitignore`
- `Dockerfile`
- `README.md`
- `alembic.ini`
- `app/__init__.py`
- `app/__main__.py`
- `app/application/__init__.py`
- `app/application/import_tree.py`
- `app/application/ports.py`
- `app/application/report.py`
- `app/bootstrap.py`
- `app/domain/__init__.py`
- `app/domain/planning.py`
- `app/domain/taxonomy.py`
- `app/infrastructure/__init__.py`
- `app/infrastructure/database.py`
- `app/infrastructure/files.py`
- `app/infrastructure/petrovich.py`
- `app/infrastructure/quality_results.py`
- `app/infrastructure/reporting.py`
- `app/observability.py`
- `app/settings.py`
- `compose.test.yaml`
- `compose.yaml`
- `docs/files-changed.md`
- `docs/import.md`
- `docs/reports/petrovich_taxonomy_import.md`
- `docs/test-matrix.md`
- `migrations/env.py`
- `migrations/versions/0001_taxonomy.py`
- `ops/check-quality.ps1`
- `pyproject.toml`
- `scripts/test.sh`
- `scripts/up.sh`
- `tests/__init__.py`
- `tests/conftest.py`
- `tests/fixtures/README.md`
- `tests/fixtures/tree.html`
- `tests/fixtures/tree.json`
- `tests/test_architecture.py`
- `tests/test_files.py`
- `tests/test_http.py`
- `tests/test_import_use_case.py`
- `tests/test_large_tree.py`
- `tests/test_planning.py`
- `tests/test_postgres.py`
- `tests/test_quality_results.py`
- `tests/test_settings_cli.py`
- `tests/test_taxonomy.py`
- `uv.lock`
