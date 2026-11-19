<!-- docs/files-changed.md -->
# Файлы доработки частичного импорта

База: `main`, `06498a5ff70130fcf30e1fe575031e00e0710810`, сверена с GitHub.
Существующие слои, ImportTree, файловый адаптер, PostgreSQL и UoW сохранены.
Добавлен CLI `import --input ... [--collector-report ...] [--apply]`; старые команды
продолжают работать. Локальный import не требует permission/review и сохраняет
complete=false. HTTP-адаптер и его ограничения не менялись.

Контракты: информационные diagnostics в snapshot, совместимый SHA старых снимков,
отдельный отчёт нормализации, source/provenance без подмены статуса, quality_flags
для последующей проверки. Аудит сохраняет исходные сведения, warnings и counts.
Миграция `0002_partial_import`: nullable review, новый categories.quality_flags
и дополнение старого baseline пустым набором flags без перезаписи ручных значений.
Новых переменных/секретов нет; комментарий PERMISSION_FILE уточнён для HTTP.

Изменены:

- `.dockerignore`
- `.env.example`
- `.gitignore`
- `README.md`
- `app/__main__.py`
- `app/application/import_tree.py`
- `app/application/ports.py`
- `app/application/report.py`
- `app/domain/taxonomy.py`
- `app/infrastructure/database.py`
- `app/infrastructure/files.py`
- `app/infrastructure/reporting.py`
- `docs/files-changed.md`
- `docs/import.md`
- `docs/reports/petrovich_taxonomy_import.md`
- `docs/test-matrix.md`
- `scripts/up.sh`
- `tests/conftest.py`
- `tests/test_import_use_case.py`
- `tests/test_postgres.py`
- `tests/test_taxonomy.py`

Созданы:

- `app/domain/quality.py`
- `docs/technical-assignment.md`
- `migrations/versions/0002_partial_import.py`
- `tests/test_partial_import.py`

Исходные `data/input.json` и `data/report.json` не изменены; data/ остаётся локальной
и передаётся на сервер отдельно. Runtime/tests/reports/cache находятся в `.tmp/`.
Коммит, push и изменение рабочей БД aistation агент не выполнял.
