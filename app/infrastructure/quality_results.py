# app/infrastructure/quality_results.py
"""Читает фактические JUnit-сводки из .tmp для эксплуатационного отчёта, не запускает тесты."""

from pathlib import Path
from xml.etree import ElementTree


def read_test_results(directory: Path) -> list[dict]:
    """Возвращает отдельные результаты запусков, не суммируя повторные тесты между ними."""
    results = []
    for path in sorted(directory.glob("pytest-*.xml")):
        try:
            root = ElementTree.parse(path).getroot()
            suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
            results.append(
                {
                    "file": path.as_posix(),
                    "tests": sum(int(s.get("tests", "0")) for s in suites),
                    "failures": sum(int(s.get("failures", "0")) for s in suites),
                    "errors": sum(int(s.get("errors", "0")) for s in suites),
                    "skipped": sum(int(s.get("skipped", "0")) for s in suites),
                    "timestamps": [s.get("timestamp") for s in suites],
                }
            )
        except (ElementTree.ParseError, OSError, ValueError):
            results.append({"file": path.as_posix(), "status": "unreadable"})
    return results
