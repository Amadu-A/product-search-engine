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

    @field_validator("artifact_dir", "permission_file", "report_file")
    @classmethod
    def check_artifact_path(cls, value: Path) -> Path:
        """Удерживает рабочие артефакты исключительно внутри .tmp проекта."""
        root = (Path.cwd() / ".tmp").resolve()
        if not value.resolve().is_relative_to(root):
            raise ValueError("Рабочие файлы разрешены только внутри .tmp/")
        return value
