# tests/test_architecture.py
"""Архитектурный gate для направлений зависимостей и русской документации кода."""

import ast
from pathlib import Path


def test_dependency_boundaries():
    """Domain/application не импортируют драйверы, transport или composition root."""
    for layer in ("domain", "application"):
        for path in Path("app", layer).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    name = node.module or ""
                    assert not name.startswith(
                        (
                            "sqlalchemy",
                            "httpx",
                            "fastapi",
                            "app.infrastructure",
                            "app.bootstrap",
                        )
                    ), (path, name)
                if isinstance(node, ast.Import):
                    assert all(a.name not in {"sqlalchemy", "httpx", "fastapi"} for a in node.names)


def test_source_documentation_and_paths():
    """Каждый исходный модуль и callable имеют документацию, файл начинается с пути."""
    for base in ("app", "tests", "migrations"):
        for path in Path(base).rglob("*.py"):
            content = path.read_text(encoding="utf-8")
            assert content.splitlines()[0] == "# " + path.as_posix()
            tree = ast.parse(content)
            assert ast.get_docstring(tree), path
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    assert ast.get_docstring(node), (path, node.name)
