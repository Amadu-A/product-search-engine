#!/usr/bin/env bash
# scripts/test.sh
# Ruff и все тесты в отдельном Docker stack с временной PostgreSQL, без production данных.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p .tmp/{reports,coverage,logs,temp,pycache,browser}
dc=(docker compose -f compose.test.yaml)
cleanup() {
    # Удаляются только временные контейнеры этого test project; volumes не используются.
    "${dc[@]}" down
}
trap cleanup EXIT
"${dc[@]}" config --quiet
"${dc[@]}" build tests
"${dc[@]}" up -d --wait --wait-timeout 120 test-postgres
"${dc[@]}" run --rm --no-deps tests -m ruff check app tests migrations
"${dc[@]}" run --rm --no-deps tests -m ruff format --check app tests migrations
"${dc[@]}" run --rm --no-deps tests
git diff --check
