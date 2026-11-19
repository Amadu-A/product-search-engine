<!-- docs/test-matrix.md -->
# Матрица различий и регрессий

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
