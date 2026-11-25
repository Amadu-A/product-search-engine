# app/settings.py
"""Layered-конфигурация CLI; пароль проверяется только для операций с БД."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Читает baseline, приватные переопределения и окружение в порядке приоритета."""

    model_config = SettingsConfigDict(
        env_file=(".env.example", ".env"), env_file_encoding="utf-8", extra="ignore"
    )
    postgres_host: str = "postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "product_search"
    postgres_user: str = "product_search_app"
    postgres_password: SecretStr = SecretStr("")
    db_connect_timeout: int = Field(default=5, ge=1, le=60)
    db_statement_timeout_ms: int = Field(default=30000, ge=1000, le=300000)
    source_base_url: str = "https://petrovich.ru"
    source_catalog_path: Literal["/catalog/"] = "/catalog/"
    http_user_agent: str = "ProductSearchTaxonomy/0.1"
    http_timeout_seconds: float = Field(default=20, ge=1, le=120)
    http_max_response_bytes: int = Field(default=8388608, ge=1024, le=16777216)
    snapshot_max_bytes: int = Field(default=67108864, ge=1024, le=134217728)
    http_min_interval_seconds: float = Field(default=2, ge=1, le=60)
    http_retries: int = Field(default=2, ge=0, le=3)
    taxonomy_max_nodes: int = Field(default=20000, ge=1, le=100000)
    taxonomy_max_depth: int = Field(default=32, ge=1, le=128)
    artifact_dir: Path = Path(".tmp/taxonomy")
    permission_file: Path = Path(".tmp/taxonomy/permission.json")
    report_file: Path = Path(".tmp/reports/petrovich_taxonomy_import.md")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    vcs_ref: str = "unknown"
    app_uid: int = Field(default=1000, ge=0)
    app_gid: int = Field(default=1000, ge=0)
    postgres_image: str = "postgres:16.15-alpine"
    python_image: str = "python:3.12-slim"
    postgres_memory_limit: str = "512m"
    app_memory_limit: str = "512m"
    test_database_url: SecretStr = SecretStr("")
    inference_mode: Literal["live", "mock"] = "live"
    job_transport: Literal["rabbitmq", "postgres"] = "rabbitmq"
    shared_vlm_base_url: str = "http://shared-vlm:8000/v1"
    shared_vlm_model: str = "shared-vlm"
    shared_vlm_model_revision: str = "unverified"
    shared_embedding_base_url: str = "http://shared-embedding:8000/v1"
    shared_embedding_model: str = "shared-embedding"
    shared_embedding_model_revision: str = "unverified"
    inference_timeout_seconds: float = Field(default=90, ge=1, le=300)
    readiness_timeout_seconds: float = Field(default=5, ge=1, le=30)
    inference_retries: int = Field(default=1, ge=0, le=3)
    inference_concurrency: int = Field(default=2, ge=1, le=8)
    inference_max_tokens: int = Field(default=1200, ge=128, le=8192)
    inference_max_input_chars: int = Field(default=16000, ge=1000, le=64000)
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: SecretStr = SecretStr("")
    qdrant_collection_alias: str = "product_taxonomy_categories_active"
    qdrant_image: str = "qdrant/qdrant:v1.19.0"
    qdrant_timeout_seconds: float = Field(default=15, ge=1, le=120)
    embedding_dimensions: int = Field(default=4096, ge=1, le=8192)
    mock_embedding_dimensions: int = Field(default=64, ge=8, le=4096)
    embedding_batch_size: int = Field(default=32, ge=1, le=128)
    taxonomy_reconcile_interval_seconds: int = Field(default=900, ge=10)
    taxonomy_index_worker_enabled: bool = True
    category_candidate_top_k: int = Field(default=10, ge=1, le=10)
    taxonomy_outbox_max_attempts: int = Field(default=5, ge=1, le=10)
    rabbitmq_host: str = "rabbitmq"
    rabbitmq_port: int = Field(default=5672, ge=1, le=65535)
    rabbitmq_user: str = "product_search_engine_app"
    rabbitmq_vhost: str = "product-search-engine"
    rabbitmq_password: SecretStr = SecretStr("")
    rabbitmq_queue: str = "product-search-engine.ingest.v1"
    worker_concurrency: int = Field(default=2, ge=1, le=2)
    worker_poll_seconds: float = Field(default=1, ge=0.1, le=60)
    worker_lease_seconds: int = Field(default=300, ge=60)
    upload_dir: Path = Path(".tmp/uploads")
    upload_max_bytes: int = Field(default=52428800, ge=1024, le=104857600)
    upload_max_rows: int = Field(default=100000, ge=1, le=100000)
    upload_max_files: int = Field(default=10, ge=1, le=10)
    upload_preview_rows: int = Field(default=20, ge=1, le=20)
    upload_max_field_chars: int = Field(default=10000, ge=100, le=10000)
    upload_max_sheets: int = Field(default=20, ge=1, le=100)
    upload_max_cells: int = Field(default=2000000, ge=100, le=5000000)
    upload_zip_max_bytes: int = Field(default=268435456, ge=1024)
    upload_zip_max_ratio: int = Field(default=100, ge=1, le=200)
    upload_ttl_seconds: int = Field(default=86400, ge=3600)
    http_bind_ip: str = "127.0.0.1"
    http_host_port: int = Field(default=0, ge=0, le=65535)
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_memory_limit: str = "1g"
    worker_memory_limit: str = "2g"
    qdrant_memory_limit: str = "1g"
    live_smoke_enabled: bool = False

    @model_validator(mode="after")
    def resolve_build_revision(self) -> "Settings":
        """Сохраняет SHA сборки, когда env_file с placeholder перекрыл image ENV."""
        build_ref = Path(".tmp/build-ref")
        if self.vcs_ref == "unknown" and build_ref.is_file():
            self.vcs_ref = build_ref.read_text(encoding="ascii").strip()
        return self

    @field_validator("source_base_url")
    @classmethod
    def check_origin(cls, value: str) -> str:
        """Разрешает только исходный HTTPS-host без пользовательских credentials."""
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.netloc != "petrovich.ru" or parsed.path:
            raise ValueError("SOURCE_BASE_URL должен быть https://petrovich.ru")
        return value

    @field_validator("artifact_dir", "permission_file", "report_file", "upload_dir")
    @classmethod
    def check_artifact_path(cls, value: Path) -> Path:
        """Удерживает рабочие артефакты исключительно внутри .tmp проекта."""
        root = (Path.cwd() / ".tmp").resolve()
        if not value.resolve().is_relative_to(root):
            raise ValueError("Рабочие файлы разрешены только внутри .tmp/")
        return value
