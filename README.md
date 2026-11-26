<!-- README.md -->
# Product Search Engine

Импорт каталогов товаров в PostgreSQL с нормализацией, распознаванием через shared
VLM, поиском категорий в проектном Qdrant и ручной проверкой. Рабочее место:
`/admin/import`, проверка: `/admin/review`, Swagger: `/docs`.
Цены из файлов сохраняются как **исторические непроверенные наблюдения**.
Ссылки магазинов не открываются, актуальные цены не вычисляются.

Категории в рабочей БД сохраняются с прежними UUID. Старый файл из 2775 узлов
`data/initial/input.json` для этапа 02 **не загружать**: текущий источник истины — SQL.
Предоставленный `data/fixtures/categories_fixture.dump` используется только тестами.

Структура: `app/domain/` — DTO и независимые правила; `app/application/` — общие
операции CLI/HTTP; `app/infrastructure/` — PostgreSQL, форматы, shared/Rabbit/Qdrant;
`app/presentation/` — FastAPI, шаблон и отдельные CSS/JS; `app/workers/` — обработка
заданий; `migrations/` — Alembic; `tests/` — регрессии и Playwright;
`docs/contracts/`, `docs/prompts/`, `docs/embedding/` — проверяемые контракты.
Все рабочие файлы/логи/кэши/скриншоты находятся в `.tmp/`.

В `.env` нужны `POSTGRES_PASSWORD` и для серверного live-режима
`RABBITMQ_PASSWORD` отдельного пользователя проекта. Не копировать туда весь baseline
и не перетирать существующий файл. Все поддерживаемые настройки описаны в
`.env.example`; Pydantic читает `.env.example` → `.env` → окружение процесса.
Compose использует те же defaults/overrides. Проверять интерполяцию через
`docker compose --env-file .env.example --env-file .env config --quiet`.
По решению владельца от 24.11.2026 авторизация UI/API сейчас отключена;
audit actor `operator_unverified` не выдаётся за проверенную личность.

На Ubuntu, после заполнения секретов (Docker Compose ≥2.24, Git, Python 3):

```bash
cd ~/projects/product-search-engine
export HTTP_BIND_IP=192.168.55.3 HTTP_HOST_PORT=8092
export APP_UID="$(id -u)" APP_GID="$(id -g)"
bash scripts/up.sh
```

Адрес и порт выше повторно проверены свободными на aistation 25.11.2026; перед первым
deploy повторно сверить `ss -lnt`. По умолчанию UI привязан к loopback со случайным
портом; `up.sh` выводит фактический адрес. На сервере с указанными override:
`http://192.168.55.3:8092/admin/import` и `http://192.168.55.3:8092/docs`.
Запуск сохраняет volumes/пароли/категории, делает backup перед миграцией 0003,
проверяет project RabbitMQ user/vhost, миграции, индекс и HTTP readiness.
Shared сервисы не перезапускаются; PostgreSQL и Qdrant не публикуются наружу.
Каталоги автоматически не загружаются.

На Windows для локального MOCK (Docker Desktop и Git Bash, секрет PostgreSQL
в собственном `.env`; отдельные VLM/embedding/RabbitMQ не нужны):

```powershell
$env:MSYS_NO_PATHCONV = '1'
& 'C:\Program Files\Git\bin\bash.exe' scripts/up.sh --mock
```

Открыть опубликованный loopback-порт из вывода. На Ubuntu тот же изолированный
режим запускается `bash scripts/up.sh --mock`. В UI виден явный баннер MOCK.
Обычный серверный режим использует `shared-vlm`, `shared-embedding` в `ai-shared`
и отдельные `product_search_engine_app` / `product-search-engine` в shared RabbitMQ.
Отсутствующие project user/vhost создаются только вместе, существующие credentials
не меняются. При конфликте ownership запуск останавливается с диагностикой.

Поддерживаются четыре входных формата. Обязательное поле — `name`.
Дополнительные: `source_key`, `brand`, `manufacturer`, `mpn`, `unit`, `quantity`,
`price`, `currency`, `price_date`, `seller`, `seller_inn`, `product_url`, `category_path`.
`price`/`quantity` — десятичные строки; отсутствующие валюту/единицу не угадываем.
Seller не становится Brand/Manufacturer. Исходные байты и raw сохраняются отдельно.

JSON `catalog_items_v1`:

```json
{"schema_version":1,"source":"supplier_example","items":[{"source_key":"row-1","name":"Кабель тестовый","unit":"м","quantity":"2","price":"12.50","currency":"RUB","price_date":"2024-11-18","category_path":["Электрика","Кабели"]}]}
```

Также принимается **исходный AITIM** без преобразования:

```json
{"source":"archive_example","total_unique_records":1,"records":[{"name":"Кабель тестовый","unit":null,"price":"12.50","date":"30.11.2024","organization":"Пример продавца","inn":null,"url":null,"matched_queries":["кабель"]}]}
```

Обычный массив JSON с произвольными ключами требует подтверждённого mapping.
XML `catalog_items_v1` (DTD/XXE/XInclude запрещены):

```xml
<catalog schema_version="1" source="supplier_example"><item><name>Кабель тестовый</name><unit>м</unit><price>12.50</price><currency>RUB</currency><category_path><part>Электрика</part><part>Кабели</part></category_path></item></catalog>
```

CSV — UTF-8/BOM; CP1251 выбирается явно. Разделители `;`, `,`, tab и quoting:

```csv
source_key;name;unit;quantity;price;currency;price_date;category_path
row-1;Кабель тестовый;м;2;12.50;RUB;2024-11-18;Электрика > Кабели
```

XLSX — выбранные листы, строка заголовков по умолчанию №1, те же колонки, что CSV.
Дата — ISO-строка или Excel date; `category_path` разделён ` > `. Формулы не
исполняются; отсутствие cached value отмечается. XLS/XLSM/SVC не поддерживаются.
Для CSV/XLSX/обычного JSON источник можно задать в UI; default — имя файла.
Готовые синтетические шаблоны всех четырёх форматов скачиваются в UI.
[Полная спецификация форматов](docs/import-formats.md).

Проверки Windows (uv и Python 3.12; полная интеграция требует Docker Desktop):

```powershell
./ops/check-quality.ps1
./ops/check-quality.ps1 -Integration
```

Ubuntu:

```bash
bash scripts/test.sh        # настоящие временные PostgreSQL/Qdrant, MOCK inference
bash scripts/test.sh --live # на aistation: реальные shared-модели, тестовые БД/индекс
```

Для полного AITIM E2E должны присутствовать локальные
`examples/aitim_200_raw.json` и `data/fixtures/categories_fixture.dump`.
`examples/` остаётся в `.gitignore`, передаётся на сервер отдельно. Без этих файлов
соответствующий offline тест помечается skipped; live-приёмка требует их наличия.
Остальные browser-тесты используют собственные данные. Assisted-разметка не является Gold.

CLI использует тот же импортёр. Windows gate выше подготавливает `.tmp/venv`:

```powershell
& .tmp/venv/Scripts/python.exe -m app import-file --input examples/aitim_200_raw.json --dry-run
```

Серверные импорт и отчёт — две явные операции; вторая выполняется после окончания
задания, UUID берётся из первой команды:

```bash
mkdir -p .tmp/uploads/bootstrap
cp examples/aitim_200_raw.json .tmp/uploads/bootstrap/
docker compose --env-file .env.example --env-file .env run --rm --no-deps app import-file --input .tmp/uploads/bootstrap/aitim_200_raw.json --profile auto --apply
docker compose --env-file .env.example --env-file .env run --rm --no-deps app import-report --job-id UUID --summary | tee .tmp/reports/catalog-pilot.json
```

Без `--apply` CLI не пишет jobs/raw/товары. HTTP upload сохраняет preview job,
а `start` ставит его в durable очередь. Ручные решения имеют приоритет при повторе;
Валидный выбор VLM автоматически назначает категорию. Оператор может изменить
назначение, и его решение сохраняется при повторной обработке. Файл mapping:
`{"fields":{"name":"Описание","price":"Стоимость"},"confirmed":true}`.
Для продолжения CLI: `import-file --job-id UUID --apply`.

Для ранее завершённого задания `reclassify --job-id UUID` покажет число строк
к повторной классификации, а `reclassify --job-id UUID --apply` поставит их в очередь.
Команда работает с сохранёнными данными задания, даже если файл загрузки истёк.
Проверка A/B/C на aistation: `bash scripts/test.sh --classification-live`.
[Реализация и проверки 02.4](docs/reports/stage-02-4.md).

[Операции, транзакции и восстановление](docs/import.md) ·
[ТЗ](docs/technical-assignment.md) · [Матрица тестов](docs/test-matrix.md) ·
[Файлы и контракты](docs/files-changed.md) · [Результаты этапа 02](docs/reports/stage-02.md).
