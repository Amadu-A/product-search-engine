# tests/test_quality_results.py
"""Проверяет, что отчёт использует реальные JUnit counts и не выдумывает отсутствующие."""

from app.infrastructure.quality_results import read_test_results


def test_junit_counts_and_missing_results(tmp_path):
    """Отсутствующий запуск даёт пустой список; counts читаются из конкретного XML."""
    assert read_test_results(tmp_path) == []
    (tmp_path / "pytest-test.xml").write_text(
        '<testsuites><testsuite tests="10" failures="1" errors="0" skipped="2" '
        'timestamp="2026-11-18T12:00:00"/></testsuites>',
        encoding="utf-8",
    )
    result = read_test_results(tmp_path)[0]
    assert (result["tests"], result["failures"], result["errors"], result["skipped"]) == (
        10,
        1,
        0,
        2,
    )


def test_malformed_junit_is_not_success(tmp_path):
    """Повреждённый XML явно помечается unreadable вместо ложного success."""
    (tmp_path / "pytest-invalid.xml").write_text("not xml", encoding="utf-8")
    assert read_test_results(tmp_path)[0]["status"] == "unreadable"
