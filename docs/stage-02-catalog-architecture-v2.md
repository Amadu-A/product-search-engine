<!-- docs/stage-02-catalog-architecture-v2.md -->

Актуализация реализации от 24.11.2026: команды общего pipeline приведены в §10;
вспомогательные standalone normalize/match/classify не создаются. Фактический статус
приёмки — `reports/stage-02.md`. По явному решению владельца авторизация UI/API сейчас
отключена; actor `operator_unverified` не является подтверждением личности.
Существующие UUID/родители не меняются при миграции 0003.
# Product Search Engine — ТЗ этапа 02, редакция 2 (20.11.2026)

**Статус:** утверждённая пользователем концепция конвейера, уточнённая в настоящем ТЗ; документ для реализации, не отчёт о готовом функционале.  
**Репозиторий:** `Amadu-A/product-search-engine`; перед изменениями сверять `main`, текущий HEAD, AGENTS.md и `Amadu-A/shared_infrasktructure/docs`.  
**Существующее состояние:** PostgreSQL `categories` + `taxonomy_snapshots` + `taxonomy_import_runs`; последняя проверка пользователя: **2 540 категорий**, 9 корней. UUID сохраняем.  
**Пилот:** `examples/aitim_200_raw.json`, 200 исходных строк; `examples/aitim_200_labeled.json` — *непроверенные* предложения, не ground truth.  
**Не входит в этап:** проверка живых URL, Playwright, обход сайтов, поиск новых актуальных предложений, автоматическая актуализация наличия и цен, Excel-обогащение и публичная витрина. Таблицы предложений и исторических цен создать, но не разрабатывать соответствующие фоновые обновления.

## 1. Утверждённая последовательность обработки

Для любого источника применяется **один и тот же доменный конвейер**:

```text
source (AITIM / прайс / сайт / API / Excel)
    ↓
RawRecord (строгая сохранность исходных данных, дедупликация по ключу и версии)
    ↓
NormalizedRecord (детерминированная нормализация имён, бренда, МPN, единиц, дат)
    ↓
FastProductLookup (ТОЛЬКО подтверждённые идентификаторы, без LLM)
    ├─ уверенное совпадение → existing Product; классификацию взять из утверждённой связи
    └─ не найден / неоднозначно
          ↓
     VLM answer_1 (тип, более общий тип, назначение, применение, характеристики + evidence)
          ↓
     ProductIdentity после answer_1 (поиск кандидатов, проверка артикула/вариантов)
          ↓
     Product (новый, только при достаточных данных; иначе unresolved staging)
          ↓
     Approved rules / Category aliases / Source mappings
          ↓ (если нет подтверждённого однозначного решения)
     Embedding query(answer_1) → Qdrant dense retrieval + SQL lexical candidates
          ↓
     Merge, filter, include semantically valid ancestors → Top-K full category paths
          ↓
     VLM answer_2 (выбрать один UUID / broader / needs_review / no_suitable_category)
          ↓
     DB validation (existing, active, assignable, correct parent meaning, no contradiction)
          ├─ допустимо и подтверждено политикой → Product.category_id + ClassificationDecision
          └─ неуверенно / нет → review_tasks / taxonomy_gaps
          ↓
     Seller → Offer → PriceObservation (только исторические записи входа в данном этапе)
```

**Исправление прежнего противоречия:** быстрый поиск до VLM допустим только по доказанным *уже имеющимся* идентификаторам. Полноценное распознавание и повторная идентификация — после `answer_1`. Qdrant и VLM выбирают **категорию**, но не доказывают равенство двух Product. Классификация не обязана ждать создания Product: у неидентифицируемой записи хранится предварительное решение на `normalized_record_id` без `product_id`.

**Правила безопасности:** исходный JSON неизменен; невозможность получить модель, категорию или продавца не удаляет raw; иерархию нельзя менять из inference; оператор имеет приоритет; решения и входные доказательства версионируются. `source.organization` — организация, указанная в экспорте, **не автоматически производитель/бренд**.

## 2. Структура проекта и файлы-контракты

```text
product-search-engine/
├── data/
│   ├── initial/              # исходный Petrovich input.json / collector_report.json
│   └── fixtures/             # утверждённый экспорт категорий / технический dump
├── examples/
│   ├── aitim_200_raw.json               # оригинал 200 строк, байты не менять
│   ├── aitim_200_labeled.json           # новая предложенная разметка v2
│   └── taxonomy_unary_24_review.json    # список веток на ручную проверку
├── docs/
│   ├── stage-02-catalog-architecture-v2.md
│   ├── contracts/
│   │   ├── catalog_schema_reference.sql # точные целевые SQL-поля, индексы, FK и CHECK
│   │   ├── answer_1.schema.json
│   │   └── answer_2.schema.json
│   ├── prompts/
│   │   ├── answer_1_product_understanding.txt
│   │   └── answer_2_category_selection.txt
│   └── embedding/
│       └── category_index_v1.json       # ТОЧНЫЙ формат текста и payload Qdrant
```

В `.gitignore` проекта сейчас игнорируются `examples/` и `data/` (проверено в GitHub main); перед коммитом согласованно изменить правила, не удаляя `.tmp/`, `.env`, секреты. Держать SQL-референс как **документ**, а не исполнять `psql -f` на рабочей БД: миграции только через Alembic. Актуальный датасет категории берётся из БД или утверждённой её фикстуры, не из старых 2 775 узлов.

## 3. Строгая PostgreSQL модель и связи

Полная DDL-спецификация в [`docs/contracts/catalog_schema_reference.sql`](contracts/catalog_schema_reference.sql). Она задаёт **точные поля, типы, NULL, FK, CHECK, уникальность, индексы**. Кодекс реализует те же контракты в миграциях, не выполняя SQL-референс напрямую. Ниже назначение таблиц и кардинальности.

| Таблица | Первичный ключ / связи | Ответственность |
|---|---|---|
| `categories` **существует** | `id UUID`, `parent_id → categories.id` | Каноническое дерево, 2 540 строк; новая `is_assignable`, статус, описания; UUID неизменен |
| `taxonomy_snapshots`, `taxonomy_import_runs` **существуют** | существующие PK/FK | История исходных снимков/импорта; не удалять |
| `brands`, `brand_aliases` | UUID, `brand_aliases.brand_id` | Бренды и проверенные алиасы `DKC` ↔ `ДКС`; кандидатов не сливать |
| `manufacturers` | UUID | Фактические изготовители; не равны по определению продавцам и брендам |
| `products` | UUID; `brand_id`, `manufacturer_id`, `category_id` nullable | Одна установленная идентичность изделия; `category_id` может быть NULL |
| `product_identifiers` | UUID; `product_id` | MPN/GTIN/EAN/SKU с issuer, статусом, доказательствами |
| `sellers` | UUID | Юрлица/продавцы; ИНН уникален только при подтверждении и в юрисдикции |
| `sources`, `source_runs` | UUID; `sources.seller_id?`, `sources.parent_source_id?`, `source_runs.source_id` | Агрегатор, оригинальный сайт и конкретный import-run — разные сущности |
| `raw_records` | UUID; `source_id`, `first_run_id` | Идемпотентное хранение неизменённого `payload`; дата источника ≠ дата скачивания |
| `normalized_records` | UUID; `raw_record_id` | Результат детерминированной нормализации по версии |
| `product_understandings` | UUID; `normalized_record_id` | Проверенный по JSON Schema `answer_1`, version/model/input hash/evidence |
| `product_match_decisions` | UUID; `normalized_record_id`, `product_id?` | Fast-key, повторная идентификация и ручные решения, отдельно от Category |
| `category_aliases`, `category_source_mappings` | UUID; `category_id`, `source_id` | Одобренные соответствия терминов и категорий источника |
| `classification_decisions` | UUID; `normalized_record_id`, `product_id?`, `category_id?` | Воспроизводимый выбор Category, точный/широкий, метод и причины |
| `taxonomy_gaps`, `review_tasks` | UUID, ссылки на запись/Product | Недостающие категории и очередь ручных проверок |
| `offers` | UUID; `product_id?`, `seller_id?`, `source_id` | Конкретное предложение, возможно неразрешённое |
| `price_observations` | UUID; `offer_id`, `raw_record_id` | Исходная цена, даже повреждённая, с единицей, датой, флагами |
| `price_history` | UUID; `offer_id`, `price_observation_id` | Только подтверждённая цена и история изменений (не актульность AITIM-2024) |
| `category_change_sets`, `category_redirects` | UUID и FK | Согласованные правки дерева, сохранение ссылок и аудита |
| `taxonomy_index_outbox`, `taxonomy_index_state` | BIGINT, SMALLINT | Durable события изменений PostgreSQL и состояние Qdrant |

**Отдельные инварианты:**

1. `products.category_id` — одна основная категория; поиск кандидатов и история выбора отдельно. `NULL` при недостатке сведений. Не создавать Product из страницы категории или рекламной подборки.
2. `brand_id` — отдельная нормализованная сущность; `manufacturer_id` отдельно, `seller_id` только в Offer. Бренд/производитель из названия — пока *кандидат*; без подтверждения не повышать до approved.
3. `brand+MPN` — **сильный кандидат**, не безусловный уникальный ключ: важны серия, комплектация, размеры, количество полюсов, номинал, ревизия и упаковка. В `product_identifiers` уникальность issuer/namespace, а не глобальная уникальность всех кодов.
4. `raw_records` неизменяемые по payload; новая версия того же source_key = новая строка по SHA. Неизменная запись при повторном импорте не создаётся заново; отдельные `source_runs` фиксируют факты запусков.
5. Тип денег `numeric`, не `float`. Валюта и единица могут отсутствовать в Observation; подтверждённая цена обязана их иметь. Продавец и цена из AITIM не означают актуальную цену. Импорт AITIM-2024 не выставляет `current_price`.
6. Статусы независимы: Product (`provisional/identified/needs_review/retired`), Category (`provisional/approved/deprecated`), Match (`confirmed/candidate/rejected/unresolved`), Classification (`proposed/approved/rejected/needs_review/no_suitable_category/superseded`). Поля `is_assignable` и `review_status` управляются администратором; число дочерних категорий не устанавливает их автоматически.
7. Новые связанные таблицы создаются без пересоздания существующих; `category_id` FK RESTRICT. Для удаляемых/объединяемых категорий требуется preview, решение пользователя, транзакционное переназначение ссылок и redirect.
8. Миграции из текущей ревизии `0002_partial_import` проверяются на копии текущей БД и на пустой БД; rollback при ошибке, без `DROP` существующих категорий, томов и снимков.

## 4. Data quality и обработка ошибок

Каждая исходная запись получает ошибки в `normalized_records.issues[]`: `code`, `field`, `raw_value`, `stage`, `severity`, `message`, `evidence_ids`, `resolution_status`. Ошибка **не останавливает весь импорт**, если запись может быть сохранена; только нарушение схемы/целостности или инфраструктурная ошибка целой транзакции блокирует её часть с сообщением.

| Входная ситуация | Обработка сейчас |
|---|---|
| `unit=null` или Java-подобное `[B@...` | сохранить raw; `unit_candidate=null`, `missing_unit` / `invalid_unit`, никаких «шт.» |
| `price="5.749.60"` | `invalid_price`, amount=null, сырой текст сохранён |
| `currency` отсутствует | `currency=null`, не угадывать RUB из домена/адреса |
| Организация/ИНН отсутствует | Seller остаётся unresolved, не подставлять производителя |
| В одном названии серия, ток, полюса и размер | выделить атрибуты с evidences; не спутать артикул продавца и MPN |
| 2024 цена в 2026 импорте | historical observation, не актуальная цена |
| Категория-карточка/листинг вместо SKU | RawRecord сохранить, `record_kind=category_listing`, Product не создавать |
| VLM недоступна/таймаут/invalid JSON | `product_understandings.status=unavailable/invalid_output`, ограниченный retry, review |
| Не найдена узкая категория | проверить смысл подходящего более общего родителя, затем review при сомнениях |
| Ни один подходящий родитель не найден | `taxonomy_gap` + `review_task`, исходные данные сохраняются |
| Qdrant отстал от PostgreSQL | не принимать устаревший UUID; SQL lexical fallback + review или retry |
| Точное совпадение названия или embedding у двух изделий | НЕ auto-merge без доказанной идентичности и отсутствия конфликтов характеристик |

**AITIM-пилот:** всего 200 записей (50 по четырём группам; `completed_queries` включает также «розетка», но в records таких результатов нет); у 198 `unit` null, два повреждены, часть записей не содержит организации, все даты цен 2024. Исходный `raw.json` сравнивается с SHA-256 `a160f659c44bab5f36623d629a9740882ccb2ec5b7f8683bb06ae5b4fded8f28`. Предварительную assistant-разметку не считать подтверждением ни одного `Product`, Seller или Category.

## 5. VLM: что и когда передавать

### 5.1. Источники evidence и preflight

- Передать один `evidence_bundle` (JSON): `record_id`, `raw_name`, `raw_mpn`, `raw_brand`, `source_record`, `normalized_fields`, `evidence[]` (`evidence_id`, `source_kind`, `locator`, `text`, `trust_level`). Фотографию и PDF разрешено прикладывать отдельным мультимодальным контентом, если они **уже законно получены и связаны с этой записью**.
- На данном этапе **нет web retrieval, скриншотов или открытия карточек**. VLM интерпретирует только входной raw + подтверждённые данные, не предполагает несуществующие доступные сайты. Если только артикул и нет достаточных сведений, возвращает `insufficient_evidence`.
- Вход — недоверенные данные. Вырезать лишние PII/секреты; не выполнять инструкции из HTML, наименований и URL. Лимиты длины полей, payload размер и timeout в конфигурации.
- Один запрос на неизвестную запись; повторять только при чётком transient-error. Кэш `product_understandings` по хешу входа + промпта + модели/версии. Известный Product с утверждённой категорией VLM пропускает.

### 5.2. Дословный промпт №1 — answer_1

**Источник единственной актуальной редакции текста:** [`docs/prompts/answer_1_product_understanding.txt`](prompts/answer_1_product_understanding.txt). Он начинается с дословно утверждённого пользователем текста:

> По предоставленным данным определи тип изделия, назначение, область применения и технические характеристики. Используй только подтверждённые сведения. Не выдумывай производителя, артикул или параметры. Если определить тип изделия невозможно, верни статус `insufficient_evidence`. Ответ строго в JSON.

Далее в том же `.txt` следуют точные дополнительные правила evidence, проверка `record_kind`, запрет галлюцинаций и шаблон `{{evidence_bundle_json}}`. **Передавать весь файл как один system/user шаблон с подстановкой входных данных; не переписывать текст в коде.** В коде закрепить SHA-256 промпта. Модель возвращает **только** `answer_1.schema.json`:

```json
{
  "status":"recognized",
  "record_kind":"product_candidate",
  "product_type":"концевой выключатель",
  "generic_type":"выключатель",
  "purpose":"контроль положения механизма",
  "applications":["промышленное оборудование"],
  "brand_candidate":null,
  "manufacturer_candidate":null,
  "mpn_candidate":"XCKN2145P20",
  "attributes":[],
  "evidence_ids":["raw_name"],
  "reason":"Тип изделия прямо указан во входном наименовании"
}
```

Это **образец допустимого формата**, не утверждение, что модель действительно обработала эту запись. При входе только `XCKN2145P20` без описания статус должен быть `insufficient_evidence`; `brand_candidate` без подтверждения — null. `status` используем вместо неинформативного булева `false`.

### 5.3. Дословный промпт №2 — answer_2

**Единственная актуальная редакция:** [`docs/prompts/answer_2_category_selection.txt`](prompts/answer_2_category_selection.txt). Начинается с дословно согласованного текста:

> Перед тобой описание изделия и список категорий из нашей БД. Выбери наиболее узкую категорию, которая действительно включает данный тип изделия. Учитывай не только название категории, но и весь путь, назначение и исключения. Можно выбрать более общую категорию. Если ни одна не подходит, верни `no_suitable_category`. Нельзя создавать категории или возвращать идентификаторы вне списка.

В конце файла два входных JSON: `{{answer_1_json}}` и `{{category_candidates_json}}`. Список категорий **не более 10**, каждая запись: UUID, `name`, полный `path_ids/path_names`, `is_active`, `is_assignable`, `review_status`, утверждённые `description/purpose/exclusions`, score+метод retrieval. Не подставлять весь справочник целиком.

Ожидаемый контракт `answer_2.schema.json`:

```json
{
  "status":"selected",
  "category_id":"066c38fc-fac7-5688-b6c5-0d9afa580cc5",
  "selection_type":"broader",
  "reason":"Концевой выключатель входит в общий тип выключателей",
  "supporting_evidence_ids":["raw_name"],
  "compared_candidate_ids":["066c38fc-fac7-5688-b6c5-0d9afa580cc5"]
}
```

UUID выше — **кандидат из предыдущей реконструкции дерева**, а не проверенный по текущей PostgreSQL факт. Уточнить при загрузке с БД. Для `needs_review` или `no_suitable_category` поле `category_id` обязательно null, `selection_type=none`.

### 5.4. Валидация ответов и приоритеты

1. Настроить structured output по JSON Schema на поддерживаемом интерфейсе **shared-vlm**; конкретный адаптер и параметры уточнить через реальный Gateway. Схема гарантирует синтаксис, но **не истинность** фактов.
2. Проверить ссылки `evidence_ids` против входного пакета; нормализовать, но не дополнять недоказанные параметры.
3. Для `answer_2` проверить, что UUID содержится в переданном Top-K, существует в PostgreSQL, активен, `is_assignable=true`, не был изменён/удалён после формирования кандидатов. Проверить совместимость смысла и родительского пути; не полагаться на численное confidence модели.
4. Человек/утверждённый Product → approved source mapping → approved category alias/rule → VLM proposal → review. Не менять ранее утверждённую классификацию от повторного импорта.
5. `selection_type=broader` **не ошибка**; `taxonomy_gap` только если не подходит ни одна допустимая более общая категория. Категория «Электрика / Розетки, выключатели / Выключатели» — допустимый кандидат для концевого выключателя *при отсутствии утверждённого ограничения области применения*. При неопределённой области — review, а не категорический запрет.

## 6. Qdrant: модель эмбеддингов и точный текстовый контракт

**PostgreSQL — единственный источник истины** для категорий. Qdrant хранит вычисляемую проекцию, которую можно полностью пересоздать.

- Инфраструктурный логический endpoint: `shared-embedding:8000/v1/embeddings`, model `shared-embedding` (конфигурация через env; **не хардкодить**). В качестве текущей предполагаемой модели — `Qwen/Qwen3-VL-Embedding-8B`. Её документация указывает до 4096 измерений, но **фактическую размерность проверять на ответе сервиса**, затем закреплять при создании коллекции. Версию, параметры truncation/normalization/instructions фиксировать и хранить в индексе.
- Qdrant не входит в реестр managed shared-сервисов проекта `shared_infrasktructure`: для данного проекта применить собственный Qdrant как отдельный контейнер/настроенный endpoint **или** подтверждённый существующий Qdrant по согласованию. Нельзя придумывать его адрес.
- Коллекция — `product_taxonomy_categories_v1_<build>`, стабильный alias для чтения — `product_taxonomy_categories_active`; один point = **одна текущая категория** (UUID Qdrant совпадает с PostgreSQL). Dense vector `dense`: cosine, размерность по реальной embedding модели (ожидаемо 4096).
- `document_version=category_doc_v1`; строгое форматирование category/query приведено в [`docs/embedding/category_index_v1.json`](embedding/category_index_v1.json). Ни категории, ни товарные поисковые запросы не должны включать цены, продавцов или рекламный текст.

**Документ для векторизации категории, дословный шаблон:**

```text
Категория: {category_name}
Полный путь: {full_path}
Синонимы: {approved_aliases}
Описание: {reviewed_description}
Назначение: {reviewed_purpose}
Исключения: {reviewed_exclusions}
```

Векторизуем **полный путь**, а не только `name`. `approved_aliases` — только утверждённые, `description/purpose/exclusions` только проверенные; если отсутствуют — каноническое «не указано». Поля сериализуются в установленном порядке. Для Qwen3-VL-Embedding query/document instruction и версия сериализации должны быть одинаково контролируемыми, без тайного изменения промпта при обновлении SDK. Для модели хранить `model_revision` и `embedding_dim`; изменение любого из них — новая коллекция.

**Поисковый запрос из `answer_1`, дословный шаблон:**

```text
Тип изделия: {product_type}
Более общий тип: {generic_type}
Назначение: {purpose}
Применение: {applications}
Ключевые характеристики: {type_disambiguating_attributes}
```

Только данные, влияющие на вид изделия; MPN и цену не включать в смысловой запрос, но использовать MPN для Product Matching. Типовые технические признаки допустимы для отсева чужих категорий. `insufficient_evidence` нельзя молча заменять якобы правильным смысловым запросом.

**Поиск:** Qdrant dense Top-20 + лексический поиск PostgreSQL по именам, полным путям и approved aliases Top-20 → RRF/dedup → расширение допустимыми родительскими категориями → Top-10 в `answer_2` с полным путём. При возможности совместимого Qdrant — гибрид dense+sparse/BM25; не делать BM25 блокирующей предпосылкой пилота. Ранжирование — лишь кандидаты, **никаких автоматических назначений по cosine-score без валидации на gold**.

**Payload (обязательные поля):** `category_id`, `parent_id`, `path_ids`, `path_names`, `depth`, `name`, `source`, `is_active`, `is_assignable`, `review_status`, `document_sha256`, `taxonomy_revision`, `aliases_version`, `document_version`, `embedding_model_revision`, `embedding_dimensions`. В ответе Qdrant проверять `category_id` в актуальном PostgreSQL. Пользовательские `approved` правила имеют приоритет.

## 7. Автоматическая синхронизация PostgreSQL Categories → Qdrant

Ни ручного запуска после изменения, ни переиндексации на каждый запрос к модели быть не должно. Схема — **транзакционный PostgreSQL outbox + идемпотентный index worker + reconciliation**.

### 7.1. Захват изменений

1. Миграция создаёт `taxonomy_index_outbox` и триггеры `AFTER INSERT/UPDATE/DELETE` для `categories`. Триггер **в той же транзакции**, что и изменение БД, пишет событие (`upsert/delete/rebuild_subtree`) с `entity_id`, `old_parent_id`, `new_parent_id`, timestamp. Это работает и при `psql DELETE`, а не только при вызове приложения.
2. Триггеры на `category_aliases` и обновления approved aliases/description вызывают переиндексацию соответствующих категорий. При изменении имени категории или `parent_id` требуется переиндексировать **всех её потомков**, поскольку меняется полный путь.
3. Outbox хранит событие до подтверждённого применения. Не писать в Qdrant из SQL-триггера и не вызывать сеть внутри транзакции PostgreSQL.
4. Рабочий процесс извлекает пакеты событий под `FOR UPDATE SKIP LOCKED`, берёт проектный advisory lock на taxonomy-index, перечитывает **актуальные** данные из PostgreSQL, строит документ, получает embedding и делает upsert/delete Qdrant с `wait=true`. После подтверждения помечает событие обработанным. На ошибке — bounded retries/backoff с `last_error`, не потерять событие.
5. Параллельные события одной категории коалесцируются. Старое событие никогда не должно перезаписывать более свежий документ: перед записью перечитывать текущее состояние и сверять `document_sha256/taxonomy_revision`; при необходимости перерассчитывать. Не полагаться лишь на порядок BIGSERIAL при параллельных транзакциях.

### 7.2. Проверка при запуске и периодический self-heal

1. При старте worker читать все **активные категории** и утверждённые aliases из PostgreSQL, сверять версию модели/document schema и контрольный SHA набора документов с `taxonomy_index_state`; при пустой/сбитой коллекции выполнить rebuild.
2. Каждые **15 минут** (конфигурируемый интервал) проверять состояние, точное множество UUID и content hash документов; расхождения → upsert отсутствующих/изменённых и delete лишних. Не использовать `indexed_vectors_count` Qdrant как точный счётчик: для сверки использовать список ID/scroll и собственные хеши.
3. При изменении `embedding_model_revision`, размерности, текстового шаблона или алгоритма retrieval создать новую версионированную коллекцию, заполнить её целиком, проверить count+IDs+hash и **атомарно переключить alias**. Старую коллекцию оставить до успешной проверки, затем удалить по политике retention.
4. `taxonomy_index_state` хранит `collection_alias`, `collection_name`, версии, checksum, `last_indexed_at`, `last_successful_reconciliation`, `status/error`; API даёт `GET /api/v1/taxonomy/index-status` и `POST /api/v1/taxonomy/reindex` (админ), не раскрывая секретов.
5. Индекс **eventually consistent**. Если Qdrant отстал, classification worker не принимает недоказанный выбор: актуальность и `is_assignable` валидируются в SQL **в транзакции перед сохранением**; при конфликте — повторный поиск или `needs_review`. Доступен лексический fallback.
6. Restore fixture в пустую БД → немедленный плановый полный reindex; удаление/слияние категории через утверждённый changeset → коррекция Product/mappings/redirect/descendants и outbox в одной транзакции. Никогда не импортировать заново старые 2 775 узлов для восстановления индекса.

### 7.3. Обязательные тесты индекса

Добавление категории → point появляется; изменение имени родителя → все дети получают новый path; перемещение ветки → old/new paths согласованы; архивирование/SQL DELETE → point исчезает; aliases approved/rejected → текст меняется; два одновременных изменения не приводят к stale overwrite; падение Qdrant → outbox сохраняется и восстанавливается; изменение embedding размерности → новая коллекция и атомарный alias switch; полный rebuild даёт точно такое же множество UUID, как PostgreSQL.

## 8. Ручное подтверждение и отсутствующая категория

Минимальный административный `/admin/review` входит в этап **02.3** (с моделями и API уже в 02.1). Интерфейс показывает raw-строку, результат `answer_1` с evidence, Product-кандидаты и причины отказа, Top-K категории с полными путями и score, итог `answer_2`, issues и статус.

- **Подтвердить Product** / **не объединять**: независимый журнал product_match_decisions.
- **Подтвердить Category**, **выбрать существующую**, **выбрать более общую**: журнал classification_decisions; правило/alias можно добавить только после отдельного утверждения.
- **Нет категории**: создаётся `taxonomy_gap` с предложенным видом и родительским кандидатом; UI позволяет оставить без категории, отклонить нецелевой тип или **предложить новую категорию** (создаёт только оператор).
- **Ветка с одним ребёнком**: не удалять автоматически. Отчёт `taxonomy_unary_24_review.json` — лишь кандидатный список, требует сравнения с текущим SQL-деревом. Утверждённый changeset обеспечивает переназначение всех связанных Products/mappings, redirect и переиндексацию.
- Все POST защищены правами, операцией идемпотентности и аудитом `(actor, time, old, new, reason)`; публичный просмотр и изменение — разные права.

## 9. Актуальность AITIM и разделение этапов

**Пока делаем:** сохранить 200 исходных записей и, где возможно, исторические price observations. Даты 2024 года не переинтерпретируются как 2026 год. Не выводить из них текущую цену, наличие или актуальность ссылки. Цена с неизвестной валютой/единицей остаётся неутверждённым наблюдением.

**Позднее отдельный этап:** инспектор товарных страниц, HTTP/DOM/JSON-LD/VLM извлечение, проверка ссылки и доступности, OfferDiscovery, обновление PriceHistory и подбор в Excel. На текущем этапе **никаких сетевых переходов и автоматических проверок** в URL AITIM, кроме ручной верификации тестового случая по желанию владельца. `price_history` сейчас существует как структура для будущего, но автоматический price-verification не входит в реализацию.

## 10. Команды, API, конфигурация

**Реализованные команды общего pipeline:**

```bash
python -m app taxonomy-singletons
python -m app import-file --input examples/aitim_200_raw.json --dry-run
python -m app import-file --input examples/aitim_200_raw.json --apply
python -m app import-report --job-id UUID --summary
python -m app review-item --item-id UUID --decision-file .tmp/decision.json --idempotency-key KEY
python -m app taxonomy-index-status
python -m app taxonomy-reindex --dry-run
python -m app taxonomy-reindex --apply
```

**API (предлагаемые):** `GET /api/v1/categories/tree`, `GET /api/v1/products`, `GET /api/v1/review/items`, `GET /api/v1/review/items/{id}`, `POST /api/v1/review/items/{id}/decisions`, `GET /api/v1/ingest-runs/{id}`, `GET /api/v1/taxonomy/index-status`, `POST /api/v1/taxonomy/reindex`, `GET /api/v1/taxonomy/singletons`. FastAPI + Swagger, Pydantic DTO. На тяжёлые операции — задания и polling, не синхронный HTTP timeout.

**Логические env (пример значений, реальные адреса/ключи в `.env`):**

```dotenv
SHARED_VLM_BASE_URL=http://shared-vlm:8000/v1
SHARED_VLM_MODEL=shared-vlm
SHARED_EMBEDDING_BASE_URL=http://shared-embedding:8000/v1
SHARED_EMBEDDING_MODEL=shared-embedding
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION_ALIAS=product_taxonomy_categories_active
QDRANT_VECTOR_SIZE=4096
TAXONOMY_RECONCILE_INTERVAL_SECONDS=900
TAXONOMY_INDEX_WORKER_ENABLED=true
CATEGORY_CANDIDATE_TOP_K=10
```

`QDRANT_URL` — **пример только для проектного контейнера**; перед deploy подтвердить фактическое размещение. Для batch use RabbitMQ shared по согласованному контракту, отдельный пользователь/vhost/лимит concurrency; не дублировать shared-vlm/shared-embedding, не подменять физические model ID логическими. Ни URL inference, ни secrets не хардкодить в доменной логике.

## 11. Реализация по подэтапам и критерии готовности

**02.1 — PostgreSQL + контракты:** новые таблицы и индексы из reference SQL реализованы Alembic, сохранено 2 540 категорий, API/DTO ошибок, decision/review, outbox triggers, SQL-audit одиночек; фикстура восстановлена на отдельной БД. Никакой мутации рабочей БД при тестах.

**02.2 — ingestion + inference + Qdrant:** импорт AITIM-200 (повтор не дублирует raw), нормализатор без галлюцинаций, fast lookup, запрос `answer_1`, Product matching, approved mappings/rules, эмбеддинг по контракту, Qdrant sync/reindex, один запрос `answer_2`, FK/category validation, все статусы+записи history. Это *работающий*, а не декларируемый путь: real integration через тестовый Gateway и ограниченный batch; offline fakes для регрессий.

**02.3 — минимальный Review UI:** ручная проверка идентичности и категории, выбор broader category и `taxonomy_gap`, audit/scope, корректировка одобренных правил, поддержка одиночек; не начинать массовую загрузку до review пилота.

**Общие acceptance criteria:**

1. 200 исходных записей, повторный ingest без увеличения числа уникальных raw; lossless по SHA; без сети.
2. Все решения Product/Category объяснимы и воспроизводимы; без MPN нет auto-merge по имени; разные токи/полюса/размеры не сливаются.
3. В VLM не отправляется полный список категорий и не выполняются пять запросов по уровням: максимум `answer_1` и `answer_2` на неизвестный товар, с кэшем и проверяемым fallback.
4. XCKN2145P20 как концевой выключатель → **кандидат** «Электрика / Розетки, выключатели / Выключатели», если не обнаружено ограничений категории; не объявлять gap из-за отсутствия подпапки «Концевые».
5. Медицинский лоток не приписывается кабельным лоткам. Нет приемлемой общей категории → gap и review.
6. Четыре технически разных выключателя IEK различаются по параметрам; цена 2024 года не current; неверная единица/цена не теряются.
7. Индекс Qdrant соответствует текущим `categories`; при ручном SQL изменения доходят до индекса, ошибки retry без потери событий; alias rebuild атомарен, SQL post-validation обязательна.
8. На 02.1 и 02.2 существуют unit/integration (реальный PostgreSQL + Qdrant)/regression/architecture тесты, Ruff, `git diff --check`, интеграционный тест нового интерфейса; результаты печатать, временные артефакты только `.tmp/`.
9. Размеченный JSON остаётся **assisted-proposal**, до ручного подтверждения точность классификатора на нём не объявлять доказанной. Для метрик создать отдельный Gold после review.
10. Не трогать рабочие volume, текущую БД и внешние shared-сервисы до отдельного deploy; Windows dev → commit/push → Linux pull/apply migration/rebuild/index health check. Изменения схемы через Alembic и только с резервной копией перед миграцией заполненной БД.

### 11.1. Дополнение к ТЗ: чего не хватает для надёжного качества

- Словарь **типов изделий и границ категорий** (`category_aliases`, `semantic_exclusions`) должен пополняться после ручного подтверждения, иначе Qdrant будет повторять семантически похожие ошибки.
- Перед первым массовым запуском вручную утвердить репрезентативный Gold из 200 строк, причём отдельно **Product Match Gold** и **Category Gold**; сравнить precision auto-accept, recall@5, долю `needs_review`, число VLM запросов и стоимость GPU/1000 записей.
- Отдельный SQL-аудит категорий с единственным ребёнком — не критерий удаления. При любой оптимизации дерева проверить все ссылки из Product, mappings, history и связанные redirects.
- Нагрузочный тест с симуляцией падений inference/Qdrant и повторной доставкой событий, без дублирования Product/Offer/Price и без утраты raw; жёстко ограничить параллелизм запросов к shared inference.

## 12. Источники и границы проверки

- Текущее состояние проверено read-only SQL через разрешённый SSH на aistation 24.11.2026: 2540 категорий, 9 корней; рабочая БД в тестах не изменялась.
- Проверен GitHub main: `app/infrastructure/database.py`, `app/domain/taxonomy.py`, `docs/technical-assignment.md`, `AGENTS.md`, `compose.yaml`, `.gitignore`; текущие дочерние изменения на Windows/сервере могут быть новее main.
- Контракт общих сервисов: `Amadu-A/shared_infrasktructure/docs/services.yaml` — логические `shared-vlm`, `shared-embedding`. Qdrant в managed shared не указан.
- Qwen3-VL-Embedding-8B: до 4096 измерений, instruction-aware — <https://huggingface.co/Qwen/Qwen3-VL-Embedding-8B>.
- Qdrant: запись точек/upsert и атомарное переключение aliases, docs <https://qdrant.tech/documentation/manage-data/collections/>; гибридный поиск <https://qdrant.tech/documentation/search/hybrid-queries/>.
- vLLM structured outputs: <https://docs.vllm.ai/en/latest/features/structured_outputs/>; конкретная поддержка в развёрнутом inference зависит от его текущей версии и настроек.
