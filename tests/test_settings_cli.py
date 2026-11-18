# tests/test_settings_cli.py
"""Контракты layered Settings и CLI-кодов, включая безопасные отрицательные сценарии."""

import logging
from pathlib import Path

import pytest

from app.__main__ import main
from app.infrastructure.database import create_db_engine
from app.observability import timed
from app.settings import Settings


def test_layered_environment(tmp_path, monkeypatch):
    """Baseline < приватный override < process; пароль не печатается в repr."""
    baseline = tmp_path / "baseline.env"
    private = tmp_path / "private.env"
    baseline.write_text("LOG_LEVEL=INFO\nDB_CONNECT_TIMEOUT=5\n", encoding="utf-8")
    private.write_text("LOG_LEVEL=WARNING\nPOSTGRES_PASSWORD=test-only-secret\n", encoding="utf-8")
    settings = Settings(_env_file=(baseline, private))
    assert settings.log_level == "WARNING" and settings.db_connect_timeout == 5
    assert "test-only-secret" not in repr(settings)
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert Settings(_env_file=(baseline, private)).log_level == "ERROR"


def test_compose_placeholder_preserves_baked_commit(tmp_path, monkeypatch):
    """Baseline unknown не скрывает SHA образа; явный runtime override сохраняет приоритет."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".tmp").mkdir()
    (tmp_path / ".tmp/build-ref").write_text("a" * 40, encoding="ascii")
    assert Settings(_env_file=None, vcs_ref="unknown").vcs_ref == "a" * 40
    assert Settings(_env_file=None, vcs_ref="b" * 40).vcs_ref == "b" * 40


def test_missing_password_fails_before_connection():
    """Пустой секрет останавливает DB-операции до открытия сети."""
    with pytest.raises(ValueError, match="POSTGRES_PASSWORD"):
        create_db_engine(Settings(_env_file=None, postgres_password=""))


def test_cli_export_validate_and_unapproved_template(monkeypatch, tmp_path, capsys):
    """CLI работает без БД; созданный шаблон никогда не является подтверждением apply."""
    artifact_dir = tmp_path / "cli-bundle"
    settings = Settings(
        _env_file=None, artifact_dir=artifact_dir, report_file=tmp_path / "reports/report.md"
    )
    monkeypatch.setattr("app.__main__.Settings", lambda: settings)
    assert main(["export", "--input", "tests/fixtures/tree.json"]) == 0
    assert main(["validate"]) == 0
    assert main(["review-template"]) == 0
    content = (artifact_dir / "review.template.json").read_text(encoding="utf-8")
    assert '"complete": false' in content and '"reviewed_at": null' in content
    assert '"total": 8' in capsys.readouterr().out


def test_petrovich_requires_document(monkeypatch, tmp_path, capsys):
    """Отсутствие документа даёт BLOCKED, сетевой клиент не вызывается."""
    settings = Settings(
        _env_file=None,
        permission_file=tmp_path / "absent.json",
        report_file=tmp_path / "reports/report.md",
    )
    monkeypatch.setattr("app.__main__.Settings", lambda: settings)
    assert main(["fetch-petrovich"]) == 2
    assert "BLOCKED" in capsys.readouterr().out


def test_artifacts_cannot_escape_tmp():
    """Операционные выходы не могут попадать в исходники или корень проекта."""
    with pytest.raises(ValueError, match=".tmp"):
        Settings(_env_file=None, artifact_dir=Path("app/generated"))


@pytest.mark.parametrize("fails", [False, True])
def test_timing_preserves_outcome_and_metadata(caplog, fails):
    """Общий sync-декоратор сохраняет результат/ошибку и пишет один ограниченный timing."""

    @timed("test_operation")
    def operation():
        """Имитирует значимую sync-операцию с успешным или ошибочным результатом."""
        if fails:
            raise RuntimeError("Синтетическая ошибка")
        return 42

    with caplog.at_level(logging.INFO):
        if fails:
            with pytest.raises(RuntimeError):
                operation()
        else:
            assert operation() == 42
    assert operation.__name__ == "operation"
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "duration_ms=" in message
    assert ("status=error" if fails else "status=success") in message
