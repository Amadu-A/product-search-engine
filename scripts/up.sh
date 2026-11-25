#!/usr/bin/env bash
# scripts/up.sh
# Один идемпотентный запуск своих сервисов; --mock не подключает shared infrastructure.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p .tmp/{taxonomy,reports,logs,temp,coverage,browser,pycache,uploads,backups}
export APP_UID="${APP_UID:-$(id -u)}"
export APP_GID="${APP_GID:-$(id -g)}"
export VCS_REF="$(git rev-parse HEAD)"
dc=(docker compose --env-file .env.example)
if [[ -f .env ]]; then
    chmod 600 .env
    dc+=(--env-file .env)
fi
mock=false
if [[ "${1:-}" == '--mock' ]]; then
    mock=true
    dc+=(-f compose.yaml -f compose.dev.yaml)
elif [[ $# -gt 0 ]]; then
    echo 'Поддерживается только bash scripts/up.sh [--mock]' >&2; exit 1
fi
docker info --format 'Docker server: {{.ServerVersion}}'
"${dc[@]}" config --quiet
if [[ "$mock" == false ]]; then
    docker network inspect ai-shared >/dev/null
fi
"${dc[@]}" build app
"${dc[@]}" up -d --wait --wait-timeout 120 postgres qdrant
revision="$("${dc[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT version_num FROM alembic_version"' 2>/dev/null || true)"
if [[ "$revision" == '0002_partial_import' ]]; then
    backup=".tmp/backups/pre-stage02-$(date -u +%Y%m%dT%H%M%SZ).dump"
    "${dc[@]}" exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup"
    test -s "$backup"
    printf 'Backup собственной БД: %s\n' "$backup"
fi
"${dc[@]}" run --rm -T --no-deps --entrypoint /app/.tmp/venv/bin/python app -m alembic upgrade head
"${dc[@]}" run --rm -T --no-deps app db-check
transport="$("${dc[@]}" run --rm -T --no-deps --entrypoint /app/.tmp/venv/bin/python app -c 'from app.settings import Settings; print(Settings().job_transport)')"
if [[ "$mock" == false && "$transport" == rabbitmq ]]; then
    # Только non-secret project identity; пароль передаётся исключительно через stdin.
    identity="$("${dc[@]}" run --rm -T --no-deps --entrypoint /app/.tmp/venv/bin/python app -c 'from app.settings import Settings; s=Settings(); assert s.rabbitmq_password.get_secret_value(), "Задайте project RABBITMQ_PASSWORD в .env"; print(s.rabbitmq_user); print(s.rabbitmq_vhost)')"
    mapfile -t project_identity <<< "$identity"
    app_user="${project_identity[0]}"; app_vhost="${project_identity[1]}"
    [[ "$app_user" == product_search_engine_* && "$app_vhost" == product-search-engine* ]] || { echo 'Неверный project Rabbit namespace'; exit 1; }
    case "$app_user" in shared_admin|shared-admin|guest|administrator) exit 1;; esac
    rabbit="$(docker ps --filter label=com.docker.compose.project=shared --filter label=com.docker.compose.service=rabbitmq --format '{{.Names}}')"
    [[ -n "$rabbit" && "$rabbit" != *$'\n'* ]] || { echo 'Не найден единственный shared RabbitMQ'; exit 1; }
    docker exec "$rabbit" rabbitmqctl ping
    users="$(docker exec "$rabbit" rabbitmqctl list_users -q)"
    vhosts="$(docker exec "$rabbit" rabbitmqctl list_vhosts -q)"
    user_exists=false; vhost_exists=false
    if printf '%s\n' "$users" | awk '{print $1}' | grep -Fxq -- "$app_user"; then user_exists=true; fi
    if printf '%s\n' "$vhosts" | grep -Fxq -- "$app_vhost"; then vhost_exists=true; fi
    if [[ "$user_exists" != "$vhost_exists" ]]; then
        echo 'Конфликт ownership RabbitMQ: project user/vhost существуют не вместе. Чужие ресурсы не изменены.'; exit 1
    fi
    if [[ "$user_exists" == false ]]; then
        "${dc[@]}" run --rm -T --no-deps --entrypoint /app/.tmp/venv/bin/python app -c 'from app.settings import Settings; print(Settings().rabbitmq_password.get_secret_value())' |
            docker exec -i "$rabbit" rabbitmqctl add_user "$app_user"
        docker exec "$rabbit" rabbitmqctl add_vhost "$app_vhost"
        docker exec "$rabbit" rabbitmqctl set_permissions -p "$app_vhost" "$app_user" '.*' '.*' '.*'
    else
        # Существующие credentials/permissions не сбрасываем; чужие ресурсы не изменяем.
        printf '%s\n' "$users" | awk -v u="$app_user" '$1==u {if ($2!="[]") exit 1}'
        permissions="$(docker exec "$rabbit" rabbitmqctl list_permissions -p "$app_vhost" -q)"
        printf '%s\n' "$permissions" | awk -v u="$app_user" 'NR>1 && NF {if ($1!=u) exit 1}'
    fi
    docker exec "$rabbit" rabbitmqctl list_permissions -p "$app_vhost"
fi
"${dc[@]}" run --rm -T --no-deps app infrastructure-check
"${dc[@]}" up -d worker api
"${dc[@]}" up -d --wait --wait-timeout 900 worker api
"${dc[@]}" exec -T api /app/.tmp/venv/bin/python -c 'import httpx,os; p=os.getenv("API_PORT","8000"); r=httpx.get("http://127.0.0.1:"+p+"/health/ready"); r.raise_for_status(); print(r.json()); r=httpx.get("http://127.0.0.1:"+p+"/admin/import"); r.raise_for_status(); assert "upload-form" in r.text; print("UI: HTTP 200")'
"${dc[@]}" ps
printf 'UI /admin/import и Swagger /docs по опубликованному адресу: '
container_port="$("${dc[@]}" exec -T api /app/.tmp/venv/bin/python -c 'from app.settings import Settings; print(Settings().api_port)')"
"${dc[@]}" port api "$container_port"
