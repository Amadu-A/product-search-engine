<!-- docs/import.md -->
# Контракт и эксплуатация импорта

## Формат и происхождение

Независимый от поставщика вход JSON показан в `tests/fixtures/tree.json`:
`source`, `complete`, `nodes`; каждый узел содержит `external_id`, `name`,
необязательные `source_url`, `children`, `kind=category`.
Только явно полное дерево проходит валидацию. Это декларация предоставившего файл,
поэтому apply дополнительно требует независимый review полного снимка.

`source` содержит `key`, `title`, `license`, `reference`, `attribution`,
`license_notice`, `usage_status`. Значения статуса:
`unverified` — права не установлены; `approved` — документированное разрешение;
`licensed` — указана применимая лицензия и сохранено уведомление;
`synthetic` — собственные тестовые данные. Не назначайте `synthetic` сторонним данным.
Для лицензированного классификатора сохраняйте точную версию/commit в reference,
лицензию, attribution и полный применимый текст license_notice.
Для Shopify Standard Product Taxonomy при необходимости требуется MIT-уведомление;
проект не зависит от его ID, версии или схемы хранения.

HTML-контракт — один `ul[data-taxonomy]`, вложенные прямые `li` с
`data-external-id`, одной прямой `a` или `span[data-name]` и необязательным `ul` детей.
Тот же контракт показан в `tests/fixtures/tree.html`. Передайте `--metadata`
с JSON `{"source": {...}, "complete": true}`. Чужая неизвестная вёрстка требует
отдельного проверенного адаптера; ссылки со всей страницы не превращаются в дерево.
Изображения и карточки товаров не запрашиваются. Поле `kind` и review не позволяют
считать произвольную товарную выгрузку достоверным классификатором.

Экспорт содержит собственный UUIDv5 в namespace `source + external_id`.
При отсутствии исходного ID необходим канонический HTTPS URL: он становится
стабильным ключом `url:<URL>`. Изменение имени/родителя не меняет UUID.
Источнику `custom` импорт запрещён: это namespace пользовательских категорий.
Корень имеет level=1; полный путь — массив названий, без неоднозначных разделителей.
Slug — стабильный ключ источника с SHA-256 внешнего ID, не зависит от транслитерации.

## Подготовка и preview — без записи категорий

Поместите законный исходный файл в `.tmp/taxonomy/input.json`. Это операторский
файл, в Git он не попадает. Для демонстрации используйте собственную фикстуру,
только в тестовом окружении: `cp tests/fixtures/tree.json .tmp/taxonomy/input.json`.
Не запускайте демонстрационные данные в рабочую БД вместо утверждённого каталога.

Команды из корня серверного checkout после `bash scripts/up.sh`:

```bash
export APP_UID="$(id -u)" APP_GID="$(id -g)"
docker compose --env-file .env.example --env-file .env run --rm --no-deps app export --input .tmp/taxonomy/input.json
docker compose --env-file .env.example --env-file .env run --rm --no-deps app validate
docker compose --env-file .env.example --env-file .env run --rm --no-deps app preview
docker compose --env-file .env.example --env-file .env run --rm --no-deps app review-template
```

Для HTML замените первую команду на:

```bash
docker compose --env-file .env.example --env-file .env run --rm --no-deps app export --input .tmp/taxonomy/input.html --metadata .tmp/taxonomy/source.json
```

`categories.json`, `categories.sha256` и `raw/<sha>.<format>` образуют единый bundle.
SHA JSON считается по каноническому JSON; raw_sha256 — по manifest, а каждый
исходный файл имеет SHA-256 точных байтов. Bundle нужен целиком при переносе.
Повреждение любого файла останавливает импорт. Модель плоского снимка описана
`app/domain/taxonomy.py`; плоский JSON можно передавать `--snapshot` в validate/preview/apply.
При ручном редактировании плоского JSON надо заново проверить дерево и сформировать
checksum и review именно этой версии; предпочтительнее исправить первичный файл и
повторить export. Полноту относительно внешнего сайта невозможно доказать одним флагом.

В `preview` видны added/updated/skipped/retained/conflicts и план before/after.
Это read-only транзакция: ни категории, ни снимок, ни audit run не записываются.

## Ручное утверждение

Скопируйте `.tmp/taxonomy/review.template.json` в `.tmp/taxonomy/review.json`.
Сверьте полноту всех ветвей, уровни и контрольные примеры с разрешённым источником.
Заполните `reviewed_by`, timezone-aware `reviewed_at` в ISO 8601, `reference` с
основанием проверки. Только после проверки установите `complete: true`.
Выборка должна охватывать каждую корневую ветвь. Шаблон по умолчанию не утверждён,
не принимается apply и не является разрешением на сторонние материалы.

Для источников с `approved`/`unverified`, а также всегда для `petrovich`, нужен
отдельный `.tmp/taxonomy/permission.json` с полями `source`, `approved_by`,
`approved_at`, `reference`, `allow_fetch`, `allow_reuse`. Его создают только при
наличии реального документа; проект не создаёт фиктивных разрешений. Отсутствие
allow_fetch не запрещает импорт отдельно законно полученного файла при allow_reuse.

## Две отдельные команды после утверждения

Первая явно изменяет БД только по утверждённому review:

```bash
docker compose --env-file .env.example --env-file .env run --rm --no-deps app apply --review .tmp/taxonomy/review.json
```

Вторая после завершения первой формирует SQL-отчёт и показывает его в терминале:

```bash
docker compose --env-file .env.example --env-file .env run --rm --no-deps app report
```

Отправьте `.tmp/reports/petrovich_taxonomy_import.md`. Несмотря на совместимое с ТЗ
имя файла, внутри явно указан фактически импортированный источник.
Для проверки идемпотентности отдельно повторите тот же apply и report: added=0,
updated=0, количество категорий не меняется. Автоматического второго apply нет.

Каждый apply атомарен: категории + полный снимок + review + условия использования
+ счётчики + commit SHA. Изменённые строки сравниваются с последним imported_values.
При ручной правке операция целиком блокируется; оператор разбирает конфликт отдельно.
Автоматического force/overwrite нет. Исчезнувшие категории и custom сохраняются.
Полный снимок и лицензия остаются в `taxonomy_snapshots.payload`; история —
`taxonomy_import_runs`. SQL хранится только в infrastructure; UoW фиксируется use-case.

Отчёт включает counts, корни, листья, глубину, уровни и корневые ветви, дубли,
циклы, потерянных родителей, недостижимые узлы, источники, до/после каждого apply,
добавлено/обновлено/пропущено/сохранено, review и результаты реальных повторов.
Наличие JUnit подтверждает только соответствующий тестовый запуск, не production import.

## БД, backup и диагностика

Нет HTTP/UI и опубликованных портов. Readiness-эквивалент этого CLI-этапа:

```bash
docker compose --env-file .env.example --env-file .env ps
docker compose --env-file .env.example --env-file .env run --rm --no-deps app db-check
```

Ожидается `status=ready`, миграция `0001_taxonomy`. Перед будущими изменениями
заполненной schema сохраните backup отдельно от Git:

```bash
mkdir -p .tmp/backups
docker compose --env-file .env.example --env-file .env exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > ".tmp/backups/catalog-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Начальная миграция создаёт только свои таблицы. Автоматический destructive downgrade
запрещён; откат заполненной БД — отдельная процедура восстановления проверенного backup.
Не удаляйте production volume. Повторный up.sh не ротирует секрет и не очищает данные.

Petrovich adapter требует реального permission, разрешающий robots.txt, публичный
DNS, ограниченные retry/timeout/размер и паузу. 401/403/429/redirect — немедленный BLOCKED,
без cookies/IP/proxy обходов. Реализован только root-запрос для будущего исследования,
не массовый обход. Полнота реального каталога и фактическая HTML-вёрстка не подтверждены.
