#!/usr/bin/env bash
# scripts/test.sh
# Ruff и все тесты в отдельном Docker stack с временной PostgreSQL, без production данных.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p .tmp/{reports,coverage,logs,temp,pycache,browser}
dc=(docker compose -f compose.test.yaml)
live=false
if [[ "${1:-}" == '--live' ]]; then
    live=true
    test -f examples/aitim_200_raw.json && test -f data/fixtures/categories_fixture.dump || { echo 'LIVE требует AITIM-200 и approved fixture dump'; exit 1; }
    docker network inspect ai-shared >/dev/null
    dc=(docker compose -p product-search-engine-live-tests -f compose.test.yaml -f compose.live-test.yaml)
elif [[ $# -gt 0 ]]; then
    echo 'Используйте bash scripts/test.sh [--live]' >&2; exit 1
fi
cleanup() {
    # Удаляются только временные контейнеры этого test project; volumes не используются.
    "${dc[@]}" down
}
trap cleanup EXIT
"${dc[@]}" config --quiet
"${dc[@]}" build tests
"${dc[@]}" up -d --wait --wait-timeout 120 test-postgres test-qdrant
"${dc[@]}" run --rm --no-deps tests -m ruff check app tests migrations
"${dc[@]}" run --rm --no-deps tests -m ruff format --check app tests migrations
if [[ "$live" == true ]]; then
    "${dc[@]}" run --rm --no-deps tests -m pytest tests/test_catalog_live.py tests/test_catalog_browser.py::test_aitim200_browser_sql_qdrant --junitxml=.tmp/reports/pytest-live-browser.xml -ra
else
    "${dc[@]}" run --rm --no-deps tests
fi
git diff --check
