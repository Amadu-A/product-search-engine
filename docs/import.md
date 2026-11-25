<!-- docs/import.md -->
# Импорт, проверка и восстановление

Текущее рабочее дерево категорий хранится в PostgreSQL. На aistation проверены
2540 категорий и 9 корней. Не восстанавливать индекс импортом старых 2775 узлов
из `data/initial/`. Дамп `data/fixtures/categories_fixture.dump` — data-only fixture
для изолированной приёмки; приложение не применяет его к рабочей БД.

## Рабочий поток

Открыть `/admin/import`, выбрать JSON/XML/XLSX/CSV. Preview показывает исходные
и нормализованные значения отдельно, профиль, SHA, строки, листы и issues.
Однозначный preview запускается автоматически при включённом переключателе.
Для неоднозначного файла подтвердить поля, кодировку, разделитель и листы.
Импорт source key не доказывает идентичность Product; offer key дополнительно
ограничен продавцом, когда он известен.

RawRecords сохраняются неизменяемыми; нормализация версионируется вместе с mapping.
Идемпотентность raw: `(source_id, source_key, payload_sha256)`; отдельно сохраняются
jobs, файлы и SourceRun каждого файла через `ingest_job_source_runs`.
Исходные байты находятся в `.tmp/uploads/`, checksum проверяется перед start.
`NormalizedRecord` не заменяет raw. Дата источника не становится датой импорта.

Рабочий путь: fast confirmed issuer/MPN → answer_1 с cache → повторная проверка
идентичности → provisional Product при достаточных данных → approved mapping/alias
или dense+SQL lexical/RRF → до 10 категорий с предками → answer_2 → SQL recheck.
Артикул без описания и листинг не создают Product автоматически. Совпадение имени,
SKU или вектора не сливает товары. Конфликт единицы/упаковки/тока/полюсов/размеров
останавливает fast merge. Brand/Manufacturer из неподтверждённого вывода модели
не подставляются в ключ идентичности без буквального evidence исходника.

Предложения VLM не утверждаются автоматически. В карточке доступны независимые
операции Product и Category, «Не является товаром», «Оставить без категории»,
создание своей категории. Назначение требует основания, expected_version и
idempotency key. Повтор ключа с другим содержимым — конфликт. Пакетное назначение
применяется только к отмеченным UUID после preview; ответ содержит applied/conflicts,
каждая строка фиксируется отдельно, незаметного overwrite нет.

## Задания и сбои

Jobs/events/checkpoints находятся в SQL. SSE поддерживает `after_id`/`Last-Event-ID`;
F5 и закрытие вкладки не запускают второй импорт. Worker ограничивает concurrency,
поддерживает lease во время inference, потерянный worker восстанавливается по lease.
На Linux уведомления передаются через project RabbitMQ с publisher confirm;
подтверждённый PG claim допускает ACK уведомления до обработки: сам job остаётся
durable, recovery и повторная публикация queued jobs сохраняют доставку.
MOCK/dev использует тот же worker с SQL transport, без второго RabbitMQ.

Cancel кооперативен; raw и уже завершённые строки сохраняются. Resume продолжает
остаток и технические ошибки. Недоступная модель и invalid output отправляют строку
на проверку, не подменяются mock-ответом. Другие строки обрабатываются дальше.
Ручная категория/исключение сохраняется при повторной загрузке. Неактивную категорию
повторно не назначаем. JSON/CSV отчёты доступны по `/report.json`, `/report.csv` и CLI
`import-report --job-id UUID`. CSV экранирует формульные префиксы.
JSON/CLI summary содержит SQL-счётчики таблиц, `duplicate_key_groups` по техническим
ключам, корни/глубину/уровни дерева и состояние индекса. Нулевые технические дубли
не означают доказанного семантического равенства/различия всех Product.

## Индекс и категории

PostgreSQL — источник истины. Триггеры категорий и aliases пишут outbox в той же
транзакции, включая прямые SQL updates. Синхронизация перечитывает SQL, пишет Qdrant
с `wait=true`, проверяет UUID, payload и content hash через scroll, только затем ACK.
Изменение родителя обновляет пути потомков; inactive ветви исключаются из проекции.
Ошибки имеют durable attempts/backoff; периодический reconciliation восстанавливает
потерянные/лишние точки. `indexed_vectors_count` не используется как точный счётчик.

Модель/размерность/template меняются через новую versioned collection, полную сверку
и атомарный alias switch. Предыдущая коллекция сохраняется; автоматического удаления
старых коллекций нет, retention выполняет оператор после проверки. При отставании
индекса — SQL lexical fallback с needs_review. Перед решением SQL повторно проверяет
существование, путь, active/assignable/status в короткой транзакции.

`taxonomy-index-status` показывает фактическое состояние.
`taxonomy-reindex --dry-run` не пишет; `taxonomy-reindex --apply` синхронизирует;
`--force` создаёт новый полный build. API reindex ставит durable сигнал worker.
Одиночные ветви: `taxonomy-singletons`, `GET /api/v1/taxonomy/singletons` — только аудит.
Изменение дерева требует отдельного `taxonomy-change --input FILE --idempotency-key KEY`,
проверки preview и `taxonomy-change-apply --change-id UUID --expected-sha256 SHA --idempotency-key KEY`.
SHA берётся из `preview_sha256` ответа предложения. Конфликт данных требует нового preview.
Старый UUID остаётся неактивным, текущие ссылки переносятся, история и redirect сохраняются.
Ни одиночки, ни имена, ни parent_id существующей рабочей БД агент автоматически не меняет.

## Конфигурация и эксплуатация

Новые таблицы и пять семантических колонок категорий — миграция
`0003_catalog_pipeline`; 0001/0002 и старые snapshot/imported_values сохранены.
Миграция не меняет имена, UUID или родителей. Raw защищён SQL-триггером, интервалы
будущей price_history — блокировкой и запретом пересечения. Текущий pipeline
price_history не заполняет.

Запуск `bash scripts/up.sh` сохраняет данные и секреты, перед 0003 делает backup
в `.tmp/backups/`. Для нового проекта сначала задать секреты; повторный запуск
пароли не генерирует и не меняет. При Rabbit ownership/credential конфликте — явный
отказ без сброса чужих ресурсов. Список пользователей/vhost читается до provisioning;
новые project credentials проверяются AMQP roundtrip в собственной временной очереди.

По умолчанию модели live, logical IDs из `.env.example`. Если точная revision
не задана, клиент фиксирует **observed-api** fingerprint `/v1/models` (id/root/created),
а не выдаёт его за weights commit. Для строгой воспроизводимости оператор задаёт
проверенные `SHARED_VLM_MODEL_REVISION` и `SHARED_EMBEDDING_MODEL_REVISION`.
В cache/decisions/index сохраняются prompt hash и фактически использованная revision.
Все запросы имеют timeout, bounded retries и semaphore; mock маркируется отдельно.

Проверки: `./ops/check-quality.ps1 [-Integration]`, `bash scripts/test.sh`,
`bash scripts/test.sh --live` на aistation. Live-тест использует настоящие shared-модели,
свои PostgreSQL/Qdrant и SQL transport; production AMQP проверяется отдельно
`infrastructure-check` при up.sh. Логи ручных команд видны в терминале.
Перед deploy сверить Docker/ss, свободный порт и shared network; smoke:
`/health/live`, `/health/ready`, `/admin/import`, `/docs`.

## Совместимость этапа 01

Старые `export/validate/preview/import/apply/report` для локальных деревьев сохранены.
Неполнота `complete=false`, UNKNOWN/unverified и отсутствие permission/review не
блокируют технический импорт. Проверки ID, родителей, циклов, глубины, атомарности
и ручных конфликтов остаются обязательными. Это совместимость механизма, а не
инструкция повторно загружать старый внешний классификатор в текущую рабочую БД.
HTTP Petrovich adapter сохраняет прежние ограничения и текущим импортом не вызывается.
