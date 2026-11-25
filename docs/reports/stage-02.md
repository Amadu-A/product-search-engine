<!-- docs/reports/stage-02.md -->
# Этап 02: реализация и фактические проверки

Дата актуализации: 25.11.2026. База разработки — `main`,
`e6a39f29b08ff56e339720bc4976b273574db89f`; локальный HEAD, GitHub и серверный
checkout сверены. Общие правила shared infrastructure перечитаны и сверены с main.
Коммит, push и развёртывание новой версии в рабочем stack агент не выполнял.

## Реализовано

- **02.1:** миграция 0003, immutable raw, версионируемая нормализация, консервативная
  идентификация Product, исторические Offer/PriceObservation, durable задания.
- **02.2:** оба shared VLM-контракта, evidence, проверка схем/UUID, cache и audit;
  dense + SQL lexical/RRF, versioned Qdrant index, transactional outbox,
  reconciliation, fallback, отдельное утверждение ручных решений.
- **02.3:** рабочие `/admin/import` и `/admin/review`, JSON/XML/XLSX/CSV,
  preview/mapping, SSE/replay после F5, остановка/продолжение, таблица и карточка,
  создание/назначение категорий, optimistic lock и bulk preview, отчёты,
  мобильные панели с восстановлением после F5, Swagger и CLI с общими use-case.
  Шаблоны скачиваются из интерфейса. Summary отчёта содержит SQL-счётчики,
  фактические группы технических дублей и уровни дерева.

Категории не пересоздаются. Миграция добавляет семантические поля и outbox,
не меняя существующие UUID, имена и parent_id. Изменение существующей таксономии —
отдельные preview/changeset/apply с hash-проверкой; автоматически оно не выполнялось.
Миграции 0001/0002 сохранены. Новые зависимости и настройки перечислены в
`pyproject.toml`, `uv.lock`, `.env.example`; границы слоёв проверяются тестами.

## Результаты проверок

| Проверка | Фактический результат | Доказательство в локальной `.tmp/` |
| --- | --- | --- |
| Windows gate: Ruff, format, unit/regression/architecture, diff | 86 passed, 30 skipped; SQL/browser/live не выдаются за выполненные этим прогоном | `logs/stage02-quality-windows-final.log` |
| `bash scripts/test.sh`: настоящие временные PostgreSQL/Qdrant, inference MOCK | 114 passed, 2 skipped, 297.38 с; оба browser E2E, включая мобильное назначение, включены; coverage 79% | `logs/stage02-test-script-final.log`, `coverage/coverage.xml` |
| Отдельный browser после исправления HTTP и обновления SSE | 2 passed, 175.63 с; insecure HTTP `pse.test`, загрузка, создание и назначение работают | `logs/stage02-browser-final.log` |
| Полный AITIM-200 через Chromium, настоящие shared VLM/embedding | 1 passed, 3212.55 с; первичная загрузка, ручное действие, повторный импорт, PostgreSQL и Qdrant | `reports/pytest-stage02-live-browser.xml`, `browser/aitim200-live/acceptance.json`, `browser/aitim200-live/01-completed.png`, `02-manual-assigned.png` |
| Короткий live smoke последней версии клиента | 1 passed, 7.04 с; оба ответа VLM, embedding 4096, поиск Qdrant, SQL | `reports/pytest-live-contract.xml`, `reports/live-contract-smoke.json` |
| Настоящий shared RabbitMQ, отдельные временные project user/vhost | 1 passed, 2.03 с; confirm/ACK, duplicate notification, настоящий worker, recovery lease, идемпотентность; inference этого транспортного теста **MOCK** | `reports/pytest-amqp-live.xml`, `reports/amqp-live-smoke.json`, `logs/stage02-amqp-launch.log` |

Полный Linux-прогон включает оба live-теста с явными skips без разрешённого
live-окружения. Их настоящие отдельные прогоны указаны в таблице. Windows gate:
86 passed, 30 skipped. CLI dry-run настоящего AITIM-200 распознал
200 строк профиля `aitim_archive_v1` без необходимости mapping и без записи в БД.

Полный live-пилот выполнен 24.11; после него внесены исправления обработки ошибок,
снимка индекса, SSE и работы UI по обычному HTTP. Клиент моделей проверен коротким
live smoke 25.11; UI, мобильные панели и SQL-статистика отчёта — изолированным
прогоном и браузером. Полный 200-строчный live-пилот после
этих исправлений повторно не запускался. Его отдельный исходный manifest сохранён в
`.tmp/reports/live-pilot-source-manifest.json`; результаты разных версий не смешиваются.

AMQP smoke создал собственные временные user/vhost с UUID-суффиксом и выдал права
только в этот vhost. После теста удалены только эти созданные ресурсы и файл секрета.
Чужие пользователи, shared admin, shared конфигурация и сервисы не изменены.
Тестовые PostgreSQL/Qdrant были изолированы от рабочего проекта.

## Что показал настоящий AITIM-200

Исходный файл не преобразован; SHA-256:
`a160f659c44bab5f36623d629a9740882ccb2ec5b7f8683bb06ae5b4fded8f28`.
Approved fixture восстановлен только в временную БД: **2540 категорий, 9 корней**.
Все их UUID сохранены. Созданная браузером синтетическая категория E2E дала ещё
одну точку: Qdrant **2541**, фактическая размерность dense-вектора **4096**.

| Счётчик | После первой загрузки и ручного назначения | После повторной загрузки |
| --- | ---: | ---: |
| RawRecord | 200 | 200 |
| Product | 181 | 181 |
| PriceObservation | 200 | 200 |
| Назначено вручную | 1 | 1 |
| needs_review | 164 | 166 |
| taxonomy_gap | 30 | 28 |
| not_product | 5 | 5 |

Первая колонка уже включает ручное назначение в синтетическую категорию и не является
оценкой точности модели. На первом проходе отмечены 198 `missing_unit`,
2 `invalid_unit`, 6 `invalid_price`, 46 `invalid_output`. Некорректные ответы не
выданы за валидную классификацию; исходные данные сохранены, строки доступны для
проверки. Различие review/gap между прогонами отражает предложения модели,
а не добавление новых raw/Product/наблюдений.

Цены 2024 года остались историческими непроверенными наблюдениями. `price_history`
не заполняется, цены «на сегодня» не вычисляются, URL продавцов не открываются.
`examples/aitim_200_labeled.json` не утверждён как Gold: метрики точности и
автоматическое бизнес-утверждение классификации не заявляются.

## Эксплуатационное состояние и ограничения

- **Implemented:** код, UI, миграция, Swagger, CLI, тесты и команды запуска.
- **Tested offline:** полный стек с настоящими PostgreSQL/Qdrant и MOCK inference.
- **Tested live:** отдельный AITIM browser pilot, текущие shared-контракты и AMQP.
- **Deployed production:** нет; серверный проект остаётся на прежнем main до
  коммита/push/pull владельцем. Рабочие 2540 категорий и 9 корней сохранены.
  Повторная SQL-проверка 25.11: MD5 упорядоченного списка UUID
  `14f5b20d95f27121360e6a4ed631f10f` совпадает с исходным; серверный Git чист.
  Доказательство — `.tmp/logs/stage02-server-final-probe.log`.
- Однокомандный MOCK запуск и повторный запуск прошли в собственном demo-stack;
  API/worker healthy, UI HTTP 200. Текущий локальный адрес после повторного запуска:
  `http://127.0.0.1:32806/admin/import`; лог `logs/stage02-up-mock-final.log`.
  Это не production deployment и не live inference в demo.
- Авторизация отсутствует по явному решению владельца. Audit actor
  `operator_unverified` не обозначает проверенную личность.
- `observed-api-*` — fingerprint ответа `/v1/models`, не commit весов. Для строгого
  воспроизведения оператор задаёт проверенные revisions в настройках.
- Один 200-строчный live pilot занял около 54 минут с повторным импортом.
  Это измерение тестового сценария, не гарантия production throughput.

Перед 0003 `up.sh` сохраняет собственный PostgreSQL backup в `.tmp/backups/`.
Деструктивный downgrade запрещён. При необходимости отката оператор останавливает
только API/worker проекта, проверяет backup в отдельной БД и отдельно согласует
восстановление; автоматического удаления новых данных/индексов нет. Проверенный
test fixture не заменяет backup рабочей БД.

## Воспроизведение

Windows: `./ops/check-quality.ps1 -Integration`. Ubuntu: `bash scripts/test.sh`.
Live VLM/embedding + AITIM browser: `bash scripts/test.sh --live` на aistation с
локальными `examples/aitim_200_raw.json` и `data/fixtures/categories_fixture.dump`.
Этот режим использует временный PostgreSQL/Qdrant и SQL transport, не AMQP.
Транспорт отдельно проверен `tests/test_rabbitmq_live.py` под собственным временным
namespace; тест требует явно предоставленных test credentials и не использует
runtime/admin credentials. `up.sh` дополнительно делает настоящий project AMQP
roundtrip через `infrastructure-check`.

Команды deployment, форматы и две операции «импорт → отчёт» приведены в
`README.md`; детали восстановления — в `docs/import.md`; полный перечень файлов —
в `docs/files-changed.md`. Старый `data/initial/input.json` повторно не импортировать.
