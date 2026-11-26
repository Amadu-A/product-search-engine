<!-- docs/test-matrix.md -->
# Матрица различий и регрессий

| Сценарий 02.4 | Проверяемое поведение | Тест |
| --- | --- | --- |
| Валидный broader, отсутствуют цена/единица/MPN/бренд | Нормализованная строка и существующая связь Product получают категорию | test_category_classification |
| Пустые кандидаты или ошибка Qdrant | Обход PostgreSQL, остановка на общей категории, no_match для неподходящего лотка | test_category_classification |
| Обрыв, JSON Schema и недопустимый UUID | Точная причина и один ограниченный запрос исправления | test_category_classification |
| Повтор старого proposed | Переиспользуются сохранённый answer_1 и Product, ручное решение сохраняется | test_category_classification |
| Автоматический и ручной итог | Раздельные счётчики в Chromium после назначения | test_catalog_browser |
| A/B/C AITIM-200 | Реальные shared VLM/embedding и временные PostgreSQL/Qdrant | test_classification_live, `bash scripts/test.sh --classification-live` |

Этап 02 дополняет, сохраняет и повторно запускает приведённые ниже проверки этапа 01.
Актуальный pilot — AITIM-200 и SQL fixture 2540 категорий, не старые 2775.
`examples/*labeled*` — assisted proposal, не Gold; точность моделей не выводится из mock.

| Сценарий этапа 02 | Проверяемое поведение | Тест |
| --- | --- | --- |
| Alembic 0003 на существующей БД | UUID/родители сохранены, DDL/outbox применены | test_postgres, test_catalog_browser |
| Все четыре формата и скачиваемые шаблоны | Одинаковые canonical поля, отдельный raw | test_catalog_formats, test_catalog_contracts |
| Дубли JSON-ключей, NaN, XXE, CSV encoding/quoting, XLSX formula | Явный отказ/issue без исполнения | test_catalog_formats, test_catalog_contracts |
| Переполнение numeric | invalid_price до SQL, другие строки не потеряны | test_catalog_contracts |
| 198 missing_unit / 2 invalid_unit | Ничего не заменяется «шт.» | test_catalog_formats |
| MPN/issuer/variant и разные seller SKU | Нет слияния по имени/SKU/вектору | test_catalog_pipeline, test_catalog_contracts |
| Недоступность/invalid output модели | Raw/наблюдения сохранены, retry/review, без ложного success | test_catalog_pipeline, test_catalog_contracts |
| Approved source mapping | Приоритет перед answer_2, VLM не auto-approve | test_catalog_pipeline |
| Повтор и ручное решение | Raw/Product/Price не дублируются, ручной приоритет | test_catalog_pipeline, test_catalog_browser |
| Несколько файлов | Отдельный SourceRun/counters каждого файла, один job | test_catalog_contracts |
| Qdrant outage | Pending outbox/backoff без ACK, восстановление | test_catalog_pipeline |
| Move/delete/aliases/dimension | UUID/hash/пути согласованы, alias переключён атомарно | test_category_index_regression |
| SQL change во время embedding | Snapshot revision не ACK-ает новое событие | test_category_index_regression |
| Одиночки и changeset | Preview без мутации, hash conflict, переназначение/redirect | test_catalog_contracts |
| Bulk и optimistic lock | Только отмеченные UUID после preview, конфликты явно | test_catalog_pipeline, test_catalog_browser |
| Swagger/SSE | DTO, idempotent start, replay Last-Event-ID | test_catalog_pipeline |
| Browser | Mapping CSV, XLSX sheet, XML, F5, category create/assign, safe DOM, мобильные панели и назначение | test_catalog_browser |
| Shared AMQP | Confirm/ACK, duplicate notification, worker/recovery; inference явно MOCK | test_rabbitmq_live, отдельный серверный smoke |

Изолированный режим: реальные PostgreSQL/Qdrant, inference MOCK. Live-режим:
`bash scripts/test.sh --live` с реальными shared VLM/embedding и отдельным SQL transport.
Рабочая БД и shared-конфигурация не изменяются. AMQP под project user проверяется
отдельно при server up.sh. Отдельный smoke `test_rabbitmq_live` проверил настоящий
worker/recovery под временным project user/vhost (результат в `reports/stage-02.md`);
его нельзя считать проверенным только по PG transport или называть проверкой моделей.

CI использует собственные постоянные фикстуры в `tests/fixtures/` и синтетические
неполные данные. Пользовательские 2775 узлов проверяются отдельно из локального
`data/`, не включаются в Git или CI и не требуют повторного HTTP-обхода.

| Различие / сценарий | Ожидаемое поведение | Проверка |
| --- | --- | --- |
| JSON и вложенный HTML одной структуры | Одинаковые UUID, связи, названия и порядок | test_files |
| Название совпало у разных родителей | Допускается | test_taxonomy |
| Повтор UUID / ID / URL | Остановка | test_taxonomy |
| Нет родителя / цикл / неверный путь / уровень | Остановка до SQL | test_taxonomy |
| Неполное дерево | Предупреждение, complete=false сохраняется; импорт разрешён | test_taxonomy, test_partial_import |
| Превышен лимит | Остановка до SQL | test_taxonomy |
| Товар / неизвестная HTML-вёрстка | Остановка | test_files |
| Изменены JSON, сырой файл или путь за пределы bundle | Остановка | test_files |
| Нет permission/review, UNKNOWN/unverified, source=petrovich | Локальный импорт разрешён, сведения сохраняются | test_import_use_case, test_partial_import, test_postgres |
| Нет разрешения внешнего HTTP-адаптера | Fetch запрещён; импорт файлов его не вызывает | test_http, test_settings_cli |
| 192 страницы, 20 ошибок, 4 redirect | Предупреждения отдельно от структурных ошибок | test_partial_import, test_postgres |
| Цена в названии / путь коллекции | Исходное имя сохраняется, flags для проверки, узел импортируется | test_partial_import, test_postgres |
| Повторные пробелы / неразрывный пробел | Отдельная нормализация before/after; оригинал неизменён | test_partial_import |
| Старый snapshot без diagnostics | Прежний checksum продолжает работать | test_partial_import |
| import без --apply / с --apply | Прежний use-case, явная запись только с флагом | test_partial_import |
| HTTP 403 / 429 / redirect / robots disallow | Нет обхода или повторного запроса | test_http |
| Первая загрузка в пустую БД | Добавлено 8, глубина 3 | test_postgres |
| Dry-run | Нет категорий, снимков и audit writes | test_postgres |
| Повторный apply | Added=0, updated=0; UUID и timestamps сохраняются | test_postgres |
| Ручное изменение imported-поля | Весь apply блокируется | test_planning, test_postgres |
| Категория исчезла из нового файла | Сохраняется без деактивации | test_postgres |
| Custom рядом с импортированными узлами | Сохраняется | test_postgres |
| Дополнение частичного дерева | Добавление нового ребёнка и обновление источника без пересоздания | test_postgres |
| Миграция прежней schema | Старые имена и baseline сохранены, ручной конфликт остаётся видимым | test_postgres |
| Отказ после всех SQL writes | Rollback категорий, снимка и аудита | test_postgres |
| Цикл в недостижимой от корня части БД | SQL-отчёт находит цикл | test_postgres |
| 5055 собственных категорий | 5 корней, 5000 листьев, уровни 5/50/5000 | test_large_tree |
| Приватный override и process env | Правильный приоритет, секрет скрыт в repr | test_settings_cli |
| Направления зависимостей / документация | Архитектурный gate | test_architecture |

Windows: `./ops/check-quality.ps1`; полный Docker-прогон — с `-Integration`.
Linux: `bash scripts/test.sh`. Интеграционные тесты никогда не используют рабочую БД:
только `taxonomy_test`, уникальная schema на каждый тест, временная PostgreSQL без порта.
Offline HTTP-contract использует MockTransport и не является живой проверкой Петровича.
