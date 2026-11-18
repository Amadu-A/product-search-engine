#!/usr/bin/env bash
# scripts/up.sh
# Идемпотентно запускает только проектную БД, применяет миграции и проверяет реальный SQL.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p .tmp/{taxonomy,reports,logs,temp,coverage,browser,pycache}
export APP_UID="${APP_UID:-$(id -u)}"
export APP_GID="${APP_GID:-$(id -g)}"
export VCS_REF="$(git rev-parse HEAD)"
dc=(docker compose --env-file .env.example)
if [[ -f .env ]]; then
    chmod 600 .env
    dc+=(--env-file .env)
fi
docker info --format 'Docker server: {{.ServerVersion}}'
"${dc[@]}" config --quiet
"${dc[@]}" build app
"${dc[@]}" up -d --wait --wait-timeout 120 postgres
"${dc[@]}" run --rm --no-deps --entrypoint /app/.tmp/venv/bin/python app -m alembic upgrade head
"${dc[@]}" run --rm --no-deps app db-check
"${dc[@]}" ps
printf '%s\n' 'БД готова. Категории не загружались; apply запускается отдельно после preview/review.'
