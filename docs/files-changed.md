<!-- docs/files-changed.md -->
# Файлы этапа 02

База: `main`, `e6a39f29b08ff56e339720bc4976b273574db89f`, сверена с GitHub.
Перечень включает изменённые файлы и необходимые новые материалы ТЗ, предоставленные
в рабочей копии. Коммит и push агент не выполнял.

Сохранены существующий импорт дерева, PostgreSQL volume, Alembic 0001/0002,
исторические snapshot/audit и UUID категорий. Общие use-case вызываются UI/API/CLI.
Контракты: входные JSON Schema/XSD, обе VLM-схемы и prompts, SQL reference,
category_index_v1; API DTO описаны Swagger. Миграция `0003_catalog_pipeline`
замораживает DDL, добавляет таблицы каталога/заданий и transactional outbox,
не переименовывает и не перемещает существующие категории.

Новые переменные, безопасные defaults и секреты перечислены в `.env.example`;
Pydantic и Compose применяют согласованный layered порядок. RabbitMQ использует
только project user/vhost; VLM/embedding — действующие shared-контракты.

## Полный перечень поставляемых файлов

### Конфигурация и запуск

- `.env.example`
- `.gitignore`
- `Dockerfile`
- `README.md`
- `compose.dev.yaml`
- `compose.live-test.yaml`
- `compose.test.yaml`
- `compose.yaml`
- `ops/check-quality.ps1`
- `pyproject.toml`
- `scripts/test.sh`
- `scripts/up.sh`
- `uv.lock`

### Приложение

- `app/__main__.py`
- `app/application/catalog_import.py`
- `app/application/catalog_ports.py`
- `app/application/taxonomy_review.py`
- `app/catalog_bootstrap.py`
- `app/catalog_cli.py`
- `app/domain/catalog.py`
- `app/infrastructure/catalog_formats.py`
- `app/infrastructure/catalog_store.py`
- `app/infrastructure/category_index.py`
- `app/infrastructure/database.py`
- `app/infrastructure/inference.py`
- `app/infrastructure/job_queue.py`
- `app/presentation/__init__.py`
- `app/presentation/api.py`
- `app/presentation/dto.py`
- `app/presentation/static/css/base.css`
- `app/presentation/static/css/style.css`
- `app/presentation/static/css/workspace.css`
- `app/presentation/static/js/api.js`
- `app/presentation/static/js/events.js`
- `app/presentation/static/js/workspace.js`
- `app/presentation/templates/import.html`
- `app/settings.py`
- `app/workers/__init__.py`
- `app/workers/catalog.py`

### Миграции

- `migrations/versions/0003_catalog_pipeline.py`

### Тесты

- `tests/catalog_fixture.py`
- `tests/test_catalog_browser.py`
- `tests/test_catalog_contracts.py`
- `tests/test_catalog_formats.py`
- `tests/test_catalog_live.py`
- `tests/test_catalog_pipeline.py`
- `tests/test_category_index_regression.py`
- `tests/test_postgres.py`
- `tests/test_rabbitmq_live.py`

### Документация и контракты

- `docs/README-import-section.md`
- `docs/contracts/answer_1.schema.json`
- `docs/contracts/answer_2.schema.json`
- `docs/contracts/catalog_items_v1.schema.json`
- `docs/contracts/catalog_items_v1.xsd`
- `docs/contracts/catalog_schema_reference.sql`
- `docs/embedding/category_index_v1.json`
- `docs/files-changed.md`
- `docs/import-formats.md`
- `docs/import.md`
- `docs/prompts/answer_1_product_understanding.txt`
- `docs/prompts/answer_2_category_selection.txt`
- `docs/reports/petrovich_taxonomy_import.md`
- `docs/reports/stage-02.md`
- `docs/stage-02-catalog-architecture-v2.md`
- `docs/stage-02-import-ui-addon.md`
- `docs/technical-assignment.md`
- `docs/test-matrix.md`

### Утверждённые входные материалы

- `data/fixtures/categories_fixture.dump`
- `renders/import-workspace-mockup.png`

## Что не включать в коммит

- `examples/` игнорируется, оригинал AITIM передаётся отдельно и не изменялся.
- `data/initial/input.json` и `data/initial/collector_report.json` — прежние входные
  файлы этапа 01. Они не изменялись, в коммит этапа 02 не предлагаются и в рабочую
  БД повторно не загружаются. Существующая правка владельца `# data/` сохранена;
  команда `git add data` здесь не подходит.
- `data/fixtures/categories_fixture.dump` — предоставленный data-only fixture,
  используемый только изолированными тестами; не backup текущей рабочей БД.
- `.tmp/`, `.env`, логи, JUnit, coverage, source manifests и браузерные артефакты
  исключены из Git. Постоянный проектный отчёт `docs/reports/stage-02.md`
  ссылается на фактические локальные доказательства, не содержит секретов.

Результаты и ограничения: `docs/reports/stage-02.md`. Команды владельца:
`README.md`, `docs/import.md`. Старый отчёт этапа 01 помечен архивным.
