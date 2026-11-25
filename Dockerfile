# Dockerfile
# Закреплённые uv.lock зависимости; CLI и тесты разделены, секреты не входят в образ.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS base
WORKDIR /app
ENV UV_CACHE_DIR=/app/.tmp/uv-cache \
    UV_PROJECT_ENVIRONMENT=/app/.tmp/venv \
    PYTHONPYCACHEPREFIX=/app/.tmp/pycache \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/app/.tmp/temp
RUN mkdir -p .tmp/temp && pip install --no-cache-dir uv==0.12.23
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

FROM base AS source
COPY app ./app
COPY docs ./docs
COPY migrations ./migrations
COPY alembic.ini .env.example ./
RUN mkdir -p .tmp/temp .tmp/reports .tmp/coverage .tmp/taxonomy .tmp/pycache \
    && chmod -R a+rX .tmp/venv && chmod 1777 .tmp .tmp/temp .tmp/pycache
ARG VCS_REF=unknown
ENV VCS_REF=${VCS_REF}
RUN printf '%s\n' "$VCS_REF" > .tmp/build-ref

FROM base AS testdeps
RUN uv sync --frozen --group dev
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.tmp/playwright
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 postgresql-client \
    && uv run playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

FROM testdeps AS tests
COPY --from=source /app/app ./app
COPY --from=source /app/docs ./docs
COPY --from=source /app/migrations ./migrations
COPY --from=source /app/alembic.ini /app/.env.example ./
COPY tests ./tests
ENTRYPOINT ["/app/.tmp/venv/bin/python"]
CMD ["-m", "pytest"]

FROM source AS runtime
USER 1000:1000
ENTRYPOINT ["/app/.tmp/venv/bin/python", "-m", "app"]
CMD ["db-check"]
