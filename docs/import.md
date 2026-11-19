<!-- docs/import.md -->
# Импорт локального частичного дерева

## Контракт

Вложенный JSON содержит `source`, `complete` и `nodes`. Узлы: `external_id`,
`name`, необязательные `source_url`, `children`, `kind=category`.
Пример формата — `tests/fixtures/tree.json`. `complete: false` разрешён при export,
validate, preview и apply, сохраняется без подмены и означает предупреждение о полноте.
Отсутствующий родитель, цикл, повтор ID/URL, неверный путь/уровень и превышение
лимитов по-прежнему запрещают запись. Все фактически полученные 2775 узлов могут
быть загружены, даже если исходный сайт обойдён не полностью.

Метаданные `source`: `key`, `title`, `license`, `reference`, `attribution`,
`license_notice`, `usage_status`. Неизвестная лицензия (`UNKNOWN`), `unverified`
и пустое уведомление сохраняются как есть, информационно. Они не меняют
активность категорий, не запрещают технический импорт и не заменяются разрешением.
Локальный импорт не читает `permission.json` и не требует `review.json`, в том
числе для `source.key=petrovich`. HTTP-адаптер имеет отдельные прежние ограничения.

Внутренние UUIDv5 и slug устойчивы относительно source + external_id.
При отсутствии ID используется канонический HTTPS URL с ключом `url:<URL>`.
Изменение имени/родителя не меняет UUID. Источник `custom` зарезервирован для
пользовательских категорий и не может быть источником массового импорта.
Корень имеет level=1, полный путь — массив названий.

## Подготовка файлов на aistation

Локальные пользовательские данные `data/` исключены из Git и Docker build context.
Передайте `data/input.json` и `data/report.json` на сервер отдельно, сохранив их байты.
После pull и `bash scripts/up.sh` из корня checkout:

```bash
export APP_UID="$(id -u)" APP_GID="$(id -g)"
mkdir -p .tmp/taxonomy
cp data/input.json .tmp/taxonomy/input.json
cp data/report.json .tmp/taxonomy/collector_report.json
sha256sum data/input.json .tmp/taxonomy/input.json
```

Для предоставленного input.json SHA-256:
`0d00832ab127bcfe5a56be8a55a000f05fbc451f976b0acbd9af6f94c8787803`.
При другой версии входа значение закономерно изменится.

## Preview и явный импорт

Без `--apply` — только нормализация, структурная проверка и read-only preview БД:

```bash
docker compose --env-file .env.example --env-file .env run --rm --no-deps app import --input .tmp/taxonomy/input.json --collector-report .tmp/taxonomy/collector_report.json
```

Для записи того же файла оператор отдельно выполняет:

```bash
docker compose --env-file .env.example --env-file .env run --rm --no-deps app import --input .tmp/taxonomy/input.json --collector-report .tmp/taxonomy/collector_report.json --apply
```

`--collector-report` необязателен. Без него полнота сверх декларации input.json
остаётся неизвестной. Ошибки JSON отчёта или неверные типы счётчиков показываются
явной ошибкой; реальные HTTP-ошибки внутри корректного отчёта — предупреждения.
Сборщик повторно не запускается и внешние HTTP-запросы не выполняются.

В preview/apply видны before/after, added/updated/skipped/retained/conflicts,
структурные метрики, complete, предупреждения и качество названий.
У предоставленного отчёта отдельно учитываются 192 необработанные страницы,
20 ошибок и 4 redirect. Эти наборы могут пересекаться и не складываются в новый
счётчик пропусков. Пути для ошибок/redirect сопоставляются с полученным деревом;
список URL всех 192 страниц в отчёте отсутствует, поэтому он не выдумывается.

## Исходники, нормализация и качество

Исходные JSON и отчёт сборщика не редактируются. Их точные байты сохраняются как
`raw/<sha>.json` и `raw/<sha>.collector.json` рядом с `categories.json`.
`categories.sha256` — SHA канонического снимка; `raw_sha256` — SHA manifest,
каждый исходный файл имеет собственный SHA-256. Проверяются все исходные файлы;
выход за пределы bundle запрещён. Старые снимки без diagnostics сохраняют прежние SHA.

Пробелы в названиях нормализуются отдельно: `.tmp/taxonomy/normalization_report.json`
содержит before/after каждой правки и причины. У текущих данных таких правок нет.
Цены не вырезаются и имена не переименовываются по эвристике.
Название с ценой получает `price_in_name`; признаки коллекции в имени/пути —
`suspected_product_collection`. Они сохраняются в `categories.quality_flags`,
снимке/аудите и отчёте, не удаляют узел и не меняют `is_active`.
Помеченные записи имеют информационное состояние needs_review; отсутствие flags
означает только отсутствие обнаруженных признаков, а не подтверждённую достоверность.
Это эвристика: возможны ложные срабатывания и пропуски.

Сохраняются и отдельные команды: `export --input ... --collector-report ...`,
`validate`, `preview`, `apply` для ранее экспортированного bundle.
`apply` не требует дополнительных файлов. Исторический `--review` необязателен,
используется только как предоставленные оператором сведения аудита.
Для HTML остаётся явный `ul[data-taxonomy]` с вложенными `li`, одной прямой `a`
или `span[data-name]` и необязательным `ul` детей; `export --metadata ...` передаёт
метаданные и фактический complete. Неизвестная вёрстка и явные `kind=product`
не превращаются в дерево автоматически.

## Атомарность, дополнение и сохранение данных

Тот же ImportTree через SqlImportUnitOfWork проверяет структуру и план,
сериализует apply, блокирует существующие строки и записывает категории,
полный снимок, происхождение, предупреждения и audit run одной транзакцией.
При любом сбое всё откатывается. Preview не пишет категории, снимки или audit runs.
Файловые артефакты preview остаются в `.tmp/`.

Повторный импорт не добавляет дубликаты и не обновляет timestamps неизменных строк.
Новые дочерние категории добавляются, изменения источника обновляются.
Исчезнувшие строки не удаляются и не деактивируются; custom сохраняются.
Текущие imported-поля сравниваются с baseline: ручное изменение блокирует весь
apply, вместо молчаливой перезаписи. Автоматического force/overwrite нет.

Миграция `0002_partial_import` делает review в аудите nullable и добавляет
quality_flags. Старый аудит сохраняется. Старые baseline дополняются только пустым
набором нового поля; фактические имена и ручные изменения не подменяются.

## Отчёт и проверки

После импорта:

```bash
docker compose --env-file .env.example --env-file .env run --rm --no-deps app report
```

Результат выводится в терминал и сохраняется в
`.tmp/reports/petrovich_taxonomy_import.md`: SQL counts, корни, листья, глубина,
уровни, ветви, дубли/циклы/потерянные родители, качество, complete и отчёт сборщика,
источник/лицензия, SHA исходника и снимка, до/после и история реальных операций.
Повтор распознаётся также по SHA исходного JSON: время нового экспорта не мешает
показать нулевые added/updated. Неполнота не превращает успешную загрузку в BLOCKED.

Ручная проверка: db-check → ready; первый apply пустой БД → 2775 добавленных;
повтор → added=0, updated=0, skipped=2775; roots=9, max_depth=5, leaves=2313;
уровни 9/151/1056/1328/231. Структурных ошибок — 0. Ожидаются предупреждения
192/20/4 и признаки качества; они не означают проверенную полноту сайта.
Отрицательные сценарии с циклами/родителями проверяйте в отдельной тестовой БД.
HTTP/UI на этом этапе нет.

## Deploy, backup и диагностика

`bash scripts/up.sh` собирает только проектный CLI, запускает проектную БД,
применяет миграции и проверяет SQL. Категории автоматически не загружаются.
`bash scripts/test.sh` — полный изолированный PostgreSQL-прогон;
`./ops/check-quality.ps1 -Integration` — Windows gate.

Перед миграцией заполненной БД сохраните backup отдельно от Git:

```bash
mkdir -p .tmp/backups
docker compose --env-file .env.example --env-file .env exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > ".tmp/backups/catalog-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Readiness: `docker compose --env-file .env.example --env-file .env run --rm --no-deps app db-check`
→ ready, revision=0002_partial_import. Повторный up.sh сохраняет данные и пароль.
Destructive downgrade запрещён; восстановление backup — отдельная операторская процедура.
Shared-сервисы, сети и host ports не изменяются.

Petrovich HTTP adapter по-прежнему требует разрешённый доступ, соблюдает robots,
allowlist/DNS, timeout/retry/размер/паузу и останавливается при 401/403/429/redirect.
Этот адаптер не используется импортом локальных файлов и в текущей задаче не запускался.
