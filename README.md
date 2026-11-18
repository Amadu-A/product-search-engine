<!-- README.md -->
# Product Search Engine — этап 1

Сервис принимает законно полученное дерево категорий из JSON или структурированного
HTML, экспортирует плоский `categories.json`, проверяет структуру и показывает план
изменений. Отдельный явный `apply` загружает утверждённый снимок в PostgreSQL.
Товары, цены, Excel и frontend в этот этап не входят. Макет сохранён в `renders/`.

Структура: `app/domain/` — правила дерева и план; `app/application/` — use-case и
порты; `app/infrastructure/` — файлы, PostgreSQL и ограниченный Petrovich adapter;
`app/bootstrap.py` — сборка зависимостей; `migrations/` — Alembic;
`tests/fixtures/` — собственные синтетические данные; `ops/`, `scripts/` — команды.
Все рабочие файлы, отчёты, кэши и логи находятся в `.tmp/`; `examples/` игнорируется Git.

В приватном `.env` нужен только `POSTGRES_PASSWORD=<случайный пароль>`.
Не копируйте `.env.example`: Pydantic читает его перед `.env`, затем окружение процесса.
Compose использует те же env-файлы и явные переменные; команды ниже передают оба слоя.
Не выводите `docker compose config` с раскрытыми значениями — используйте `--quiet`.

Первый запуск на Linux (Docker Compose ≥ 2.24, Git, Python 3):

```bash
cd ~/projects/product-search-engine
python3 -c 'import os,secrets; p=".env"; os.path.exists(p) or os.fdopen(os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),"w").write("POSTGRES_PASSWORD="+secrets.token_hex(32)+"\n")'
bash scripts/up.sh
```

Повторный запуск — только `bash scripts/up.sh`: пароль, volume и категории сохраняются.
Запускается проектная БД, применяются миграции, проверяется реальное SQL-подключение.
Категории автоматически не загружаются. Host ports отсутствуют: для CLI не нужен
внешний HTTP endpoint. Shared-сервисы, сети и соседние проекты не используются.

Проверка Windows (нужен `uv` и Python 3.12):

```powershell
./ops/check-quality.ps1
# Полный прогон с настоящей временной PostgreSQL в Docker:
./ops/check-quality.ps1 -Integration
```

Если Windows блокирует запуск скрипта политикой текущего процесса:
`powershell -NoProfile -ExecutionPolicy Bypass -File ./ops/check-quality.ps1 -Integration`.
Скрипт сохранён с UTF-8 BOM для Windows PowerShell 5.1.
На Linux: `bash scripts/test.sh` — Ruff, форматирование и все тесты в отдельном
временном Docker stack. Проверки выводят логи в терминал и возвращают ненулевой код
при ошибке; JSON/coverage/JUnit артефакты остаются в `.tmp/`.

[Порядок экспорта, preview, ручного утверждения, импорта и отчёта](docs/import.md).
[Матрица проверок](docs/test-matrix.md).
[Полный перечень файлов и контрактов этапа](docs/files-changed.md).
[Статус источника Петрович](docs/reports/petrovich_taxonomy_import.md).

Источник Петрович не скопирован: разрешения нет, прямой запрос `robots.txt` получил
HTTP 403. Адаптер не обходит ограничения и не выполняет массовый обход.
Для разработки достаточно собственных фикстур; Shopify taxonomy не подключалась.
