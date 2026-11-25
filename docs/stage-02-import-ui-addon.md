<!-- docs/stage-02-import-ui-addon.md -->

Актуализация реализации от 24.11.2026: UI/Swagger/CLI используют общий импортёр;
фактические команды, DTO, кардинальность multi-file runs и результаты проверок
описаны в README, `import.md`, `reports/stage-02.md`. По прямому решению владельца
авторизация сейчас отключена, несмотря на исходные требования admin scope ниже.
Actor — `operator_unverified`; технические проверки не отключены.
# Product Search Engine — обязательное дополнение к ТЗ этапа 02 v2
## Импорт файлов, рабочий интерфейс, ручная классификация и воспроизводимая приёмка

**Редакция:** 1.0 · 20.11.2026  
**Статус:** спецификация к реализации, не утверждение о готовом коде.  
**Применять вместе с:** `docs/stage-02-catalog-architecture-v2.md`, `docs/contracts/catalog_schema_reference.sql`, `docs/contracts/answer_1.schema.json`, `docs/contracts/answer_2.schema.json`, `docs/prompts/*`, `docs/embedding/category_index_v1.json`.  
**Приоритет:** явные утверждения владельца → основной этап 02 v2 → это дополнение для UI/импорта; если технический контракт основного ТЗ не допускает требований дополнения, согласовать минимальную совместимую миграцию, не создавать параллельный pipeline.

## 1. Что именно должно быть готово при сдаче этапа

Ключевой пользовательский сценарий: открыть `/admin/import`, перетащить файл каталога JSON/XML/XLSX/CSV, получить предпросмотр и сопоставление полей (при необходимости), автоматически начать обработку корректно распознанного файла (или вручную запустить после настройки mapping), **в реальном времени видеть судьбу каждой позиции**, дождаться завершения обработки, открыть список спорных/неразнесённых позиций, назначить существующую категорию либо создать новую и назначить её. Изображение интерфейса — `renders/import-workspace-mockup.png` (визуальный ориентир, не замена рабочего UI).

**Сдача только API, Swagger, CLI и тестов не считается завершением данного дополнения.** Требуются работающий браузерный интерфейс, рабочий backend, SQL-хранение и integration/E2E проверки. UI не должен быть демо с захардкоженными результатами.

**Границы:** нет обхода магазинов, извлечения новых цен из сайтов, проверки живых ссылок, мониторинга актуальности предложений, публикации товарной витрины или заполнения Excel ценами. Строка прайса и архивная цена из загруженного документа могут сохраняться как `price_observations` по правилам основного ТЗ, но НЕ становятся текущими ценами. В интерфейсе не должно быть ложного признака «Цена актуальна».

## 2. Функциональная компоновка рабочего экрана

**Маршрут:** `/admin/import`; навигация: «Импорт», «Категории», «Проверка», «История»; существующий публичный каталог — отдельная будущая задача.

### 2.1 Левая колонка (примерно 27–32% ширины desktop)

1. **Drag & drop**: область с клавиатурной альтернативой «Выбрать файл», перечислением `JSON · XML · XLSX · CSV`, максимального размера, ограничений и допустимых профилей. Поддержать одиночный и пакетный выбор; каждый файл создаёт отдельный upload record, один import job может иметь несколько файлов. Не начинать запись Product до завершения проверки структуры; если формат распознан и mapping однозначен, включённый по умолчанию «Автозапуск после проверки» ставит работу в очередь автоматически.
2. **Список файлов:** имя, размер, профиль распознавания, статус первичной проверки, ошибки; удаление из набора до запуска.
3. **Поддерживаемые структуры:** раскрывающиеся мини-примеры каждого формата, ссылка на `docs/import-formats.md` / доступную в UI страницу помощи, объяснение: `name` обязателен, остальные поля допускают `null`; для AITIM отдельный профиль.
4. **Предпросмотр и mapping:** распознанный профиль, для XLSX — выбор листа/листов, для CSV — кодировка/разделитель/заголовки, для всех адаптеров — сопоставление полей, 10–20 строк с сырыми значениями и обнаруженными issues. Колонки `name`, `brand`, `mpn`, `unit`, `price`, `currency`, `price_date`, `seller`, `seller_inn`, `product_url`, `source_key`, `category_path`.
5. **Кнопки:** «Проверить структуру» → «Начать обработку» (только при валидном предпросмотре), переключатель «Автозапуск после проверки» (по умолчанию включён при однозначной структуре), «Остановить» / «Продолжить» для запущенного задания с понятной семантикой.

### 2.2 Правая часть — верхняя зона (live progress)

- Заголовок «Обработка номенклатуры», `run_id`, число всего/обработано/завершено/неоднозначно/без категории/с ошибками; полоска прогресса означает **реально завершённые** элементы, а не долю времени VLM.
- Журнал в виде структурированных событий, не сплошного выводимого текста: время, тип, изделие, операция, результат, причина, ссылка «Подробнее». События: «Читаю строку», «Проверяю идентификаторы», «VLM определила тип», «Нашёл Product / не нашёл», «Получено 7 кандидатов», «Выбрана категория», «Передано на проверку». Не обещать и не показывать «мысли» модели или внутренний chain-of-thought; отображать только финальные поля `answer_1`, `answer_2` и проверяемые evidence.
- Сведения о выборе категории **предварительные**, пока не выполнена SQL-валидация и не записан `classification_decisions` со статусом `approved`. Индикаторы `Предложено`, `Подтверждено`, `На проверке`, `Категория отсутствует` различать визуально.
- Переключатели «Все события / Успешные / На проверке / Ошибки»; поиск по имени/record_id; авто-прокрутка только если пользователь находится у конца ленты; восстановление ленты после F5.

### 2.3 Правая часть — нижняя зона (Review workbench)

- Список неразнесённых и спорных строк слева: название, номер файла/листа/строки, статус, причина; фильтры `Без категории`, `Нужна проверка Product`, `Входная ошибка`, `Не товар`, `Все`.
- В центре карточка выбранной строки: **оригинал** (не редактировать), `NormalizedRecord`, `answer_1` (тип, назначение, применение, параметры, evidence), кандидаты существующих Product с причиной, Top-K категорий с полным путём, предложение `answer_2` и объяснение, issues. Разрешить пользователю изменить выбранную категорию **не изменяя** исходный файл и сырую запись.
- Справа компактное дерево `categories` с поиском (по имени, пути, alias), раскрытием/сворачиванием, выделением выбранной папки, количеством Product, состояниями `active`, `is_assignable`, `review_status`. Поиск из PostgreSQL; Qdrant только формирует кандидатов для ИИ, **не рисует дерево**.
- Действия: `Назначить выбранную категорию`, `Подтвердить предложение`, `Не является товаром`, `Оставить без категории`, `Создать категорию`, `Подтвердить Product`/`Не объединять`. Независимые действия для идентичности Product и классификации. Пользователь не обязан сначала создавать новую категорию.
- «Создать категорию»: диалог с обязательным именем, выбранным `parent_id` (либо корень), `is_assignable` и обоснованием, проверкой одинакового имени среди siblings. Только полномочный администратор может создать и утвердить новую категорию; созданная категория должна быть доступна для назначения **после подтверждённой SQL-транзакции**. Утверждённое изменение создаёт outbox и дойдёт до Qdrant асинхронно; UI показывает «Синхронизация индекса», а не фиктивное «готово» до ACK.
- В нижней зоне должны быть `prev/next` по позициям, пакетное назначение **только** после явного выбора пользователем и предпросмотра затрагиваемых строк. Никакого автоматического изменения всех похожих товаров из-за одного ручного назначения.

### 2.4 Адаптивность и доступность

- Desktop ≥1280 px: левая колонка/основная область, в Review 3 панели; на 900–1279 px — дерево в выдвижной панели; mobile — вкладки «Файл», «Обработка», «На проверке», «Категории».
- Keyboard-поддержка загрузки/выбора папок/диалогов, контраст, `aria-live=polite` для итогового статуса без озвучивания сотен событий, безопасный `textContent`, не инъектировать пользовательские названия в HTML. Следовать `shared_infrasktructure/docs/FRONTEND_GUIDELINES.md`; интерактивные события — отдельные JS-модули, никаких inline `onclick`.

## 3. Форматы импорта: строгий контракт и профили

**Документировать в README проекта** компактные примеры **всех четырёх форматов** и ссылку на полный `docs/import-formats.md`. Там определены структуры, обязательность, алиасы, числовые поля и профиль AITIM. Образцы положить в `examples/import_formats/`, парсеры обязаны принимать их все.

### 3.1 Общая входная модель

Одна строка/запись в любом формате конвертируется в один `CanonicalInputItem`:

| Поле | Тип после парсинга | Обязательное | Правило |
|---|---|---|---|
| `name` | string | да | Непустое название; иначе issue и item rejected, не блокировать все остальные строки |
| `source_key` | string/null | нет | ID/артикул **источника**, не автоматически MPN изготовителя |
| `brand` | string/null | нет | Предполагаемый бренд, подтверждение отдельно |
| `manufacturer` | string/null | нет | Предполагаемый изготовитель, не равно Seller |
| `mpn` | string/null | нет | Предполагаемый артикул изготовителя; автоматическая связь только после проверки |
| `unit` | string/null | нет | Не угадывать при отсутствии |
| `quantity` | decimal string/null | нет | Количество из исходного документа; `null` при отсутствии |
| `price` | decimal string/null | нет | Цена сырая, нормализация Decimal, не подменять ошибку числом |
| `currency` | ISO 4217 string/null | нет | Не выводить RUB из языка или адреса |
| `price_date` | ISO `YYYY-MM-DD`/null | нет | Дата источника цены; дата загрузки отдельная |
| `seller` | string/null | нет | Продавец/организация, не изготовитель |
| `seller_inn` | string/null | нет | Строка, не integer, чтобы не терять нули |
| `product_url` | URL/null | нет | Исходная ссылка, **на этом этапе не открывать** |
| `category_path` | list[str]/null | нет | Категория источника, подсказка, не каноническая категория |

Все значения из оригинала сохранять как `raw_records.payload`; дополнительная информация о ячейке/листе/строке/запросе и исходном файле — в `source_runs`/`ingest_job_files` и trace locator. Если пользователь сопоставил не все колонки, ничего не удалять из raw.  `null` не означает ноль, пустая строка нормализуется только в derived-слое.

### 3.2 Форматный контракт

- **JSON:** профиль `catalog_items_v1`, корень `{ "schema_version": 1, "source": "user_upload", "items": [ ... ] }`; JSON Schema `docs/contracts/catalog_items_v1.schema.json`. `items[].name` обязательно, остальные поля опциональны. Второй поддерживаемый профиль — **AITIM archive** с оригинальными `records`, `total_unique_records`, `organization`, `date`, `matched_queries`: определить автоматически или дать выбрать, не переписывать исходный JSON.
- **XML:** профиль `catalog_items_v1`, `<catalog schema_version="1" source="user_upload"><item>...</item></catalog>`; список категорий `<category_path><part>...</part></category_path>`; XSD `docs/contracts/catalog_items_v1.xsd`; отключить DTD, entity resolution, XInclude, внешние ресурсы (XXE). Не выполнять XSLT из файла.
- **XLSX:** один или несколько выбранных листов, строка заголовков выбирается пользователем (по умолчанию №1); одна позиция = одна строка; `category_path` в ячейке разделён ` > `; файл `.xlsx` без макросов, `.xls` и `.xlsm` по умолчанию не принимаются. `openpyxl` в производственном коде через `read_only=True`, `data_only=True`, разумные лимиты листов/строк/ячеек. Формулы не исполнять; для формулы без сохранённого вычисленного значения — issue `formula_without_cached_value`.
- **CSV:** UTF-8/UTF-8-BOM по умолчанию; CP1251 — только явный выбор/надёжно определённая кодировка с подтверждением; `;`, `,`, tab — автоопределение с предпросмотром и возможностью исправить; RFC-совместимое quoting с двойными кавычками; пустые поля → null в derived. **Расширение `.svc` не поддерживается: правильное название — `.csv` (Comma-Separated Values)**. Не исполнять строки, начинающиеся с `=`, `+`, `-`, `@`, как формулы при экспорте/отображении.

Для неканонических входов поддержать **ручное сопоставление колонок/ключей** и сохранение профиля mappings по `(source, filename pattern/format)` только после явного действия оператора. Не заставлять все поставщиков использовать один формат; но после парсинга у всех единая доменная модель.

### 3.3 Лимиты и ошибки

Configurable defaults для приёмки: размер файла 50 MiB, 100 000 строк на файл, 10 файлов в job, preview 20 строк, 10 000 символов на одно поле, queue concurrency 2 задачи, VLM concurrency по лимитам shared; каждое значение переопределяемо через `.env.example`. На тестовом наборе 200 строк все проходят intake, даже если часть не является Product. При реальных нагрузках лимиты подтвердить perf-тестами. Валидация MIME + сигнатур, path traversal, ZIP-bomb у XLSX, неверного XML, невалидного UTF-8, огромных полей, потока с недоказанным charset; ограничить CPU/память, не доверять расширению. Одна плохая строка не обрывает весь файл, если её можно пропустить с отчётом.

## 4. API — минимальный обязательный контракт

Обязательно FastAPI + Swagger/OpenAPI; общий принцип: `202 Accepted` для длительных операций; UI работает на API, не обращается напрямую к PostgreSQL/Qdrant/GPU. Не создавать отдельный второй pipeline для CLI: вызовы UI и CLI идут через общие use cases из этапа 02 v2.

| Endpoint | Назначение |
|---|---|
| `POST /api/v1/import-jobs` multipart | Сохранить файл(ы), создать job в `uploaded`, вернуть `job_id`, имена, SHA |
| `GET /api/v1/import-jobs/{id}/preview` | Автоопределение формата, листов, заголовков, 20 строк, issues и предложенный mapping |
| `PUT /api/v1/import-jobs/{id}/mapping` | Явно сохранить выбор профиля, sheet/header/encoding/delimiter и mapping с `mapping_version`; при неоднозначности требует подтверждения до автозапуска |
| `POST /api/v1/import-jobs/{id}/start` | Идемпотентно поставить в очередь, вернуть `202` и текущий статус; вызывать UI автоматически после валидного preview при разрешённом автозапуске или по кнопке |
| `GET /api/v1/import-jobs/{id}` | Статус и реальные счётчики, ссылки на результаты и errors |
| `GET /api/v1/import-jobs/{id}/events?after_id=N` | SSE с сохраняемыми последовательными событиями; поддержать `Last-Event-ID` |
| `GET /api/v1/import-jobs/{id}/items?...` | Пагинированные позиции, поля, Product/Category/Review статусы |
| `GET /api/v1/import-jobs/{id}/items/{item_id}` | Подробная карточка raw/normalized/answer_1/candidates/answer_2/evidence/issues |
| `POST /api/v1/import-jobs/{id}/cancel` | Кооперативная остановка, без потери обработанных данных |
| `POST /api/v1/import-jobs/{id}/resume` | Продолжить остаток с checkpoint; idempotent |
| `GET /api/v1/categories/tree` | Актуальное дерево из PostgreSQL, поиск, `is_assignable`, `review_status` |
| `POST /api/v1/categories` | Создать новую категорию (только администратор), с audit/outbox |
| `GET /api/v1/review/items?...` | Список unresolved/needs_review/taxonomy_gap |
| `POST /api/v1/review/items/{id}/decisions` | Назначение категории / Product-решение, обязательные `expected_version`, `reason`, idempotency key |
| `GET /api/v1/taxonomy/index-status` | Актуальность Qdrant, pending outbox, последнее успешное reconcile |

Выгрузка отчёта `GET /api/v1/import-jobs/{id}/report.json` и `report.csv`: `source_file`, `sheet`, `row`, `raw_name`, `product_id?`, `category_id?`, `category_path?`, `status`, `issue_codes[]`, `decision_method`, `review_task_id?`. CSV-отчёт — результат, не входной контракт.

**Нельзя принимать фактические результаты до прохождения SQL-валидации.** `POST /start` с тем же idempotency key не создаёт второе задание; повторная загрузка того же файла не увеличивает число идентичных RawRecords. Job/run сохраняет наблюдения и события для корректной реконструкции UI после перезагрузки браузера.

### 4.1 Статусы

- Job: `uploaded → validating → preview_ready → queued → running → completed|completed_with_issues|failed|canceled`, а при отмене — `cancel_requested`; возвращаться на `running` через `resume` только для unfinished. `completed_with_issues` допустим, если есть `needs_review`/`taxonomy_gap`, и **не означает ошибку всего файла**.
- Item: `pending`, `normalizing`, `looking_up_product`, `understanding`, `matching`, `classifying`, `assigned`, `needs_review`, `taxonomy_gap`, `not_product`, `invalid_record`, `error`.
- Category decision: сохранять **статусы основного ТЗ** (`proposed`, `approved`, `needs_review`, `no_suitable_category` и т.д.), не подменять status Product.

### 4.2 Событие SSE, пример формы

```json
{
  "event_id": 145,
  "job_id": "00000000-0000-4000-8000-000000000001",
  "occurred_at": "2026-11-20T10:25:00Z",
  "type": "category_proposed",
  "level": "info",
  "item_id": "00000000-0000-4000-8000-000000000002",
  "source_name": "Выключатель XCKN2145P20",
  "stage": "category_selection",
  "message": "Предложена категория «Выключатели»",
  "data": {
    "category_id": "066c38fc-fac7-5688-b6c5-0d9afa580cc5",
    "category_path": ["Электрика", "Розетки, выключатели", "Выключатели"],
    "decision_status": "proposed"
  }
}
```

Пример — **иллюстративный**, UUID и дата не являются фактическим результатом текущей БД. Каждый event с monotonically increasing `event_id` per job или глобальным идентификатором; `after_id` не должен пропускать события при reconnect, дубликаты по ID клиент дедуплицирует. Реальный API документировать в OpenAPI со схемой DTO. В `message` запрещено раскрывать внутренние промпты, секреты, рассуждения модели, личные данные, произвольный HTML.

## 5. Минимальные новые данные для работы UI

**Таблицы из основного ТЗ оставляем без изменения смысла.** Добавить минимальные инфраструктурные таблицы через Alembic (не исполнять reference DDL напрямую):

| Таблица | Точный минимальный состав | Ограничения |
|---|---|---|
| `ingest_jobs` | `id UUID PK`, `source_run_id UUID NULL FK source_runs.id`, `created_by TEXT NULL`, `status VARCHAR(32) NOT NULL`, `mapping_version INT NOT NULL DEFAULT 1`, `total_count INT NOT NULL DEFAULT 0`, `processed_count INT NOT NULL DEFAULT 0`, `assigned_count INT NOT NULL DEFAULT 0`, `needs_review_count INT NOT NULL DEFAULT 0`, `taxonomy_gap_count INT NOT NULL DEFAULT 0`, `error_count INT NOT NULL DEFAULT 0`, `cancel_requested BOOLEAN NOT NULL DEFAULT false`, `created_at TIMESTAMPTZ NOT NULL`, `started_at TIMESTAMPTZ NULL`, `finished_at TIMESTAMPTZ NULL`, `updated_at TIMESTAMPTZ NOT NULL`, `last_error_code TEXT NULL` | CHECK counters >=0, `processed_count<=total_count`; `status` enum/check; не использовать `total_count` как число Product |
| `ingest_job_files` | `id UUID PK`, `job_id UUID NOT NULL FK ingest_jobs.id`, `original_filename TEXT NOT NULL`, `content_sha256 CHAR(64) NOT NULL`, `byte_size BIGINT NOT NULL`, `format VARCHAR(16) NOT NULL`, `profile VARCHAR(64) NULL`, `storage_key TEXT NOT NULL`, `mapping JSONB NULL`, `preview JSONB NULL`, `created_at TIMESTAMPTZ NOT NULL` | storage вне Git и webroot; положительный размер, ограничить имена; максимум по config |
| `ingest_job_items` | `id UUID PK`, `job_id UUID NOT NULL FK ingest_jobs.id`, `file_id UUID NOT NULL FK ingest_job_files.id`, `row_number INT NULL`, `sheet_name TEXT NULL`, `raw_record_id UUID NULL FK raw_records.id`, `normalized_record_id UUID NULL FK normalized_records.id`, `product_id UUID NULL FK products.id`, `category_id UUID NULL FK categories.id`, `status VARCHAR(32) NOT NULL`, `issue_codes JSONB NOT NULL DEFAULT '[]'`, `processing_version INT NOT NULL DEFAULT 1`, `updated_at TIMESTAMPTZ NOT NULL`, `created_at TIMESTAMPTZ NOT NULL` | UNIQUE(job_id,file_id,sheet_name,row_number) с NULL-aware ключом или отдельным `item_position`; FK, idempotent per mapping |
| `ingest_job_events` | `event_id BIGINT GENERATED ALWAYS AS IDENTITY PK`, `job_id UUID NOT NULL FK ingest_jobs.id`, `item_id UUID NULL FK ingest_job_items.id`, `occurred_at TIMESTAMPTZ NOT NULL`, `type VARCHAR(64) NOT NULL`, `stage VARCHAR(64) NULL`, `level VARCHAR(16) NOT NULL`, `message TEXT NOT NULL`, `data JSONB NOT NULL DEFAULT '{}'` | индекс `(job_id,event_id)` для SSE/reconnect; безопасная сериализация |

В отличие от `source_runs` (прохождение импортируемого источника), `ingest_jobs` описывает **пользовательский запрос на загрузку**, его файлы и прогресс. Сохранить связи через `source_run_id`; не дублировать исторические Product/Price или сами сырые payload. Для согласованного `source_runs` при разбиении на несколько файлов продумать дочерние runs либо `ingest_job_source_runs(job_id,source_run_id)` вместо одного поля; Codex должен использовать один вариант и описать реальную кардинальность, не оставляя два конкурирующих механизма.

**Локальная работа с файлами:** физический upload сначала в `.tmp/uploads/` (в Git игнорируется), постоянные `raw_records` в PostgreSQL; TTL сырых загрузок по env и policy, удаление физического файла не должно удалять raw/decisions. Для набора 200 записей возможность воспроизведения обеспечивается репозиторным `examples/aitim_200_raw.json`.

## 6. Оркестрация и GPU

- **Windows development:** Docker для изолированной PostgreSQL и проектного Qdrant. Тесты unit/integration/e2e с fake `VlmClient`, `EmbeddingClient` и предопределёнными deterministic responses — без GPU и без доступа к `aistation`. Реальный live-smoke (отдельно) может использовать доступные из Windows опубликованные LAN/VPN endpoints; hostname `shared-vlm` в Windows обычно не резолвится — он существует в контейнерной сети `ai-shared` на сервере.
- **Linux aistation:** project API, frontend, worker, project PostgreSQL и project Qdrant (если его ещё нет) подключить к shared-vlm / shared-embedding / RabbitMQ по существующим контрактам. Не запускать вторую копию inference, не менять shared topology без разрешения. Потолок concurrency и backpressure согласовать с Gateway/нагрузкой соседних проектов.
- **CLI:** предложить `python -m app import-file --input ... --profile auto --apply`, `--dry-run`, `--mapping-file` и `--job-id`; CLI и HTTP используют один use-case. Эти команды **новые и пока не существуют**; адаптировать к актуальному CLI проектным паттерном.
- **Qdrant:** индекс синхронизировать из SQL через утверждённый outbox + worker + reconcile; UI может показать индекс временно в состоянии `sync_pending`. Создание новой категории после одобрения происходит транзакционно в SQL, даже если Qdrant недоступен, а задачи классификации до синхронизации применяют SQL validation/fallback.
- **Очередь:** для тяжёлых заданий использовать существующую shared RabbitMQ на Linux; isolated локальный RabbitMQ лишь в тестовом/dev профиле, не устанавливать его как второй продакшен shared broker.
- **Разделение:** producer-парсер, ingestion use case, normalizer, identity service, inference adapter, category retrieval, category decision, reviewer, event emitter; не объединять SQL, FastAPI и вызовы VLM в одной функции.

## 7. Ошибки, безопасность, источники истины

1. Формат не распознан → UI предлагает выбор профиля и сопоставление; неизвестное расширение → точная ошибка. `name` пустое → конкретная строка с issue. `unit="[B@..."` → invalid_unit; `price="5.749.60"` → invalid_price; не выдумывать значения.
2. Вход AITIM — архив с ценами 2024; нельзя назвать их актуальными. `organization` не равен manufacturer/brand. Если source/brand/mpn не доказаны, Product match `needs_review`.
3. VLM вернула некорректную JSON Schema → bounded retry по технической причине, item needs_review или error, pipeline остальных строк продолжается. Модели не передавать весь каталог — только Top-K.
4. Дедупликация по raw source key + hash/version; два файла одного товара могут создать два RawRecords и один Product/несколько Offers только при доказанной идентичности; не сливать по названию. Разные товары одной категории остаются разными Product.
5. Права: гостю можно видеть только предусмотренные публичные данные. Загрузка файлов, изменение категорий, принятие Product/Category — защищённый admin scope; выбор способа auth согласовать с текущей инфраструктурой (сеть alone не достаточная защита для неограниченного upload). Лимиты rate/file size, запрет HTML/JS injection, секреты не показывать в логах.
6. Никаких незаявленных сетевых переходов на `product_url` при импорте. Ошибка URL не исключает RawRecord; HTTP status/availability и live-price — отдельный будущий этап.
7. Сохранить `actor`, `request_id`, `job_id`, prompt hash/model revision, `mapping_version`, evidence, SQL classification decision. Ручное решение пользователя имеет приоритет; повтор импорта не перезаписывает его.

## 8. Приёмочные сценарии: что сможет проверить владелец

### 8.1 Рабочий запуск

1. На Windows развернуть dev/test stack с PostgreSQL, Qdrant и API/UI; `GET /health`, Swagger и `/admin/import` открываются. Demo mock-инференс делает проверяемые, маркированные как mock результаты без GPU.
2. На Linux — развернуть настоящие `api+worker+qdrant` **после согласованного deploy** и проверить доступность shared inference. Разрешённый admin через браузер загружает `examples/aitim_200_raw.json`.
3. Проверить, что обнаружен AITIM profile, parsed `records=200`, источник 2024, `unit` ошибки показаны; убедиться в автозапуске (или нажать «Начать обработку», если mapping требует подтверждения), дождаться состояния `completed`/`completed_with_issues`. Видеть журнал и долю прогресса, F5, reconnect SSE, переход к другой странице и возвращение без потери событий.
4. Найти XCKN2145P20 (если в исходной выгрузке) и увидеть evidence/`answer_1`, кандидаты, соответствующую существующую категорию «Выключатели» либо проверку без auto-approve при отсутствии достаточного evidence.
5. Найти медицинский лоток, убедиться что он **не** назначен в электрические лотки; вручную выбрать существующую категорию или создать новую и назначить; открыть дерево и убедиться, что оно реально изменилось.
6. После создания категории `GET /api/v1/taxonomy/index-status` показывает queued/indexed; после ACK новая категория появляется в активном Qdrant alias, категория Product ссылается на PostgreSQL UUID.
7. Загрузить тот же файл ещё раз; уникальных `raw_records` не больше, решения пользователя сохранены; создаются отдельные job/event logs. Обработанные строки сохраняются при cancel/retry.
8. Пройти тем же UI по `examples/import_formats/sample_items.json`, `.xml`, `.csv`, `.xlsx`; результаты эквивалентны по canonical полям. Экспортировать `report.json`/`report.csv` и сверить category/status.

### 8.2 Тестовая матрица

- **Unit:** JSON profiles, XML secure parser/XXE, XLSX read-only + листы/формулы, CSV BOM/CP1251/delimiter/quoted-field, schema validation, column mapping, Decimal parsing, statuses/progress, key normalization, safety.
- **Integration:** PostgreSQL+Alembic миграции на пустой и заполненной БД, реальный Qdrant test container, outbox on insert/update/move/delete categories, ingestion idempotency, `source_runs` linkage, SSE event replay, concurrent review optimistic lock.
- **Contract:** FastAPI OpenAPI, response schemas, подстановки к двум дословным промптам v2, JSON Schema VLM, embedding contract `category_index_v1`.
- **Browser E2E (Playwright):** drag-drop, Excel sheet select, column mapping, start, progress events, reconnect after F5, unresolved review, category search/create/assign, errors/ACL. Не утверждать готовность по одному screenshot.
- **Live smoke на aistation:** на ограниченном наборе с реальными shared-vlm + embedding, один реальный Qdrant upsert/search, отдельные health checks, bounded concurrency; полный AITIM-200 по разрешённому тестовому окну.
- **Regression:** строгое сохранение UUID существующих categories, 2 540 категорий до пользовательских изменений (не хардкодить в приложении), отсутствие внезапного `Product` для листинга, неактуальность цен 2024, отсутствие категории у неподходящего медицинского лотка, broad fallback у концевого выключателя, отсутствие фальшивого success если VLM недоступна.

### 8.3 Обязательный отчёт Codex при сдаче

- Реально запускаемые команды Windows PowerShell и Ubuntu (не псевдокоманды), URLs страницы и Swagger, имена сервисов Compose; `docker compose ps`/health, `GET index-status`.
- Перечень DB миграций, тестов с числом passed/failed, логи (артефакты в `.tmp/`), screenshots UI и видео/последовательность E2E при необходимости.
- Демонстрация импорта **реального файла пользователя** на test DB, таблица статусов всех 200 строк, число RawRecords / Products / assigned / unresolved / issues, примеры реальных `answer_1`, Qdrant кандидатов, `answer_2`, проверка сохранения после перезапуска.
- Отдельная маркировка `MOCK` против `LIVE`: успешный тест с фейковой моделью **не доказывает**, что работает удалённая GPU inference.
- Вся реализация следует `AGENTS.md`, `shared_infrasktructure/docs/LLM_CONTEXT.md`, `ENGINEERING_GUIDELINES.md`, `FRONTEND_GUIDELINES.md`, `INFRASTRUCTURE_INSTRUCTIONS.md`, `services.yaml`; не создавать тестовые дампы/логи вне `.tmp/`, не добавлять их в Git.

## 9. Порядок реализации без фиктивной готовности

Для единой сдачи допустимы внутренние вехи:

- **02.1:** миграции + модели таблиц v2 и UI jobs/events, контракт JSON/XML/XLSX/CSV, CLI импорт/preview.
- **02.2:** backend ingestion + PostgreSQL, Qdrant, VLM через контракты, stream-events; unit/integration; мок-режим.
- **02.3:** полный рабочий UI + review + создание категорий + Swagger + E2E + live-smoke на Linux.

Нельзя объявлять всё дополнение «готовым» после реализации только 02.1. После окончания каждой вехи демонстрируется рабочая сквозная операция в предусмотренной для неё среде.
