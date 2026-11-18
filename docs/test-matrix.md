<!-- docs/test-matrix.md -->
# Матрица различий и регрессий

`examples/` пуст, golden-файлов владельца сейчас нет. CI использует собственные
постоянные фикстуры в `tests/fixtures/`; внешние данные не копируются.

| Различие / сценарий | Ожидаемое поведение | Проверка |
| --- | --- | --- |
| JSON и вложенный HTML одной структуры | Одинаковые UUID, связи, названия и порядок | test_files |
| Название совпало у разных родителей | Допускается | test_taxonomy |
| Повтор UUID / ID / URL | Остановка | test_taxonomy |
| Нет родителя / цикл / неверный путь / уровень | Остановка до SQL | test_taxonomy |
| Неполное дерево / превышен лимит | Остановка | test_taxonomy |
| Товар / неизвестная HTML-вёрстка | Остановка | test_files |
| Изменены JSON, сырой файл или путь за пределы bundle | Остановка | test_files |
| Review чужого SHA / неполная выборка | Apply запрещён | test_taxonomy |
| Нет лицензии / разрешения Петровича | Apply/fetch запрещены | test_taxonomy, test_settings_cli |
| HTTP 403 / 429 / redirect / robots disallow | Нет обхода или повторного запроса | test_http |
| Первая загрузка в пустую БД | Добавлено 8, глубина 3 | test_postgres |
| Dry-run | Нет категорий, снимков и audit writes | test_postgres |
| Повторный apply | Added=0, updated=0; UUID и timestamps сохраняются | test_postgres |
| Ручное изменение imported-поля | Весь apply блокируется | test_planning, test_postgres |
| Категория исчезла из нового файла | Сохраняется без деактивации | test_postgres |
| Custom рядом с импортированными узлами | Сохраняется | test_postgres |
| Отказ после всех SQL writes | Rollback категорий, снимка и аудита | test_postgres |
| Цикл в недостижимой от корня части БД | SQL-отчёт находит цикл | test_postgres |
| 5055 собственных категорий | 5 корней, 5000 листьев, уровни 5/50/5000 | test_large_tree |
| Приватный override и process env | Правильный приоритет, секрет скрыт в repr | test_settings_cli |
| Направления зависимостей / документация | Архитектурный gate | test_architecture |

Windows: `./ops/check-quality.ps1`; полный Docker-прогон — с `-Integration`.
Linux: `bash scripts/test.sh`. Интеграционные тесты никогда не используют рабочую БД:
только `taxonomy_test`, уникальная schema на каждый тест, временная PostgreSQL без порта.
Offline HTTP-contract использует MockTransport и не является живой проверкой Петровича.
