# tests/test_partial_import.py
"""Регрессии локального частичного импорта, происхождения и независимой диагностики качества."""

import hashlib
import json

import pytest

from app.__main__ import main
from app.application.import_tree import ImportTree
from app.domain.quality import category_quality_flags
from app.domain.taxonomy import TaxonomyError, canonical_hash, snapshot_hash, validate_tree
from app.infrastructure.files import load_snapshot, normalize_file, write_json
from app.settings import Settings
from tests.test_import_use_case import MemoryUow


def test_collector_warnings_do_not_become_structural_errors(partial_snapshot):
    """192/20/4 сохраняются отдельно; неполнота не меняет успешную проверку структуры."""
    result = validate_tree(partial_snapshot)
    assert result["errors"] == 0 and result["complete"] is False
    assert result["collection"]["remaining_pages"] == 192
    assert result["collection"]["errors"] == 20
    assert result["collection"]["redirects"] == 4
    assert {w["code"]: w.get("count") for w in result["warnings"]} == {
        "incomplete_tree": None,
        "unprocessed_pages": 192,
        "collector_errors": 20,
        "collector_redirects": 4,
    }


@pytest.mark.parametrize(
    "name,path,flags",
    [
        ("Цемент", ("Материалы", "Цемент"), []),
        ("Трубы, ледорубы, рубашки", ("Инструмент",), []),
        ("Плиткаот 1 200 ₽ / м²", ("Отделка",), ["price_in_name"]),
        (
            "Линия (Тест)от 500 руб. / м²",
            ("Отделка",),
            ["price_in_name", "suspected_product_collection"],
        ),
        ("Серия А", ("Коллекции аксессуаров", "Серия А"), ["suspected_product_collection"]),
    ],
)
def test_quality_flags_are_informational(name, path, flags):
    """Признаки не превращают трубы в цены и не удаляют подозрительные названия."""
    assert category_quality_flags(name, path) == flags


def test_normalization_preserves_original_and_prices(partial_input, tmp_path):
    """Исходные байты неизменны; пробелы нормализуются отдельно, цена остаётся в названии."""
    path, report = partial_input
    data = json.loads(path.read_bytes())
    data["nodes"][0]["children"][0]["children"][0]["name"] = "  Серия\u00a0(Тест)от 500 ₽  "
    write_json(path, data)
    before = path.read_bytes()
    report_before = report.read_bytes()
    output = tmp_path / "quality"
    snapshot = normalize_file(path, output, 8388608, collector_report_path=report)
    assert path.read_bytes() == before and report.read_bytes() == report_before
    node = next(n for n in snapshot.nodes if n.external_id == "cement")
    assert node.name == "Серия (Тест)от 500 ₽"
    assert node.imported_values()["quality_flags"] == [
        "price_in_name",
        "suspected_product_collection",
    ]
    transformed = json.loads((output / "normalization_report.json").read_text(encoding="utf-8"))
    assert transformed["normalization_count"] == 1 and transformed["complete"] is False
    assert snapshot.diagnostics["input_sha256"] == hashlib.sha256(before).hexdigest()
    assert load_snapshot(output / "categories.json", 8388608) == snapshot


def test_old_snapshot_checksum_remains_compatible(snapshot, tmp_path):
    """Добавление diagnostics не ломает SHA исторического снимка без нового поля."""
    old = snapshot.model_copy(update={"diagnostics": None})
    historical = old.model_dump(mode="json", exclude={"diagnostics"})
    assert snapshot_hash(old) == canonical_hash(historical)
    path = tmp_path / "bundle/categories.json"
    write_json(path, historical)
    path.with_suffix(".sha256").write_text(snapshot_hash(old), encoding="ascii")
    assert load_snapshot(path, 8388608).diagnostics is None


def test_json_duplicates_and_input_output_collision_are_rejected(tmp_path):
    """Техническая защита сохраняется: неоднозначный JSON и перезапись исходника запрещены."""
    path = tmp_path / "bad.json"
    path.write_text('{"complete": true, "complete": false}', encoding="utf-8")
    with pytest.raises(TaxonomyError, match="Повтор ключа"):
        normalize_file(path, tmp_path / "bad-bundle", 8388608)
    with pytest.raises(TaxonomyError, match="совпадает"):
        normalize_file(tmp_path / "categories.json", tmp_path, 8388608)


@pytest.mark.parametrize("apply", [False, True])
def test_import_cli_uses_existing_use_case_without_confirmation(
    monkeypatch,
    tmp_path,
    partial_input,
    capsys,
    apply,
):
    """Новая команда вызывает прежний ImportTree; лишние файлы подтверждения не читаются."""
    input_path, report = partial_input
    settings = Settings(
        _env_file=None,
        artifact_dir=tmp_path / "cli-bundle",
        report_file=tmp_path / "reports/report.md",
        permission_file=tmp_path / "invalid-permission.json",
    )
    settings.permission_file.write_text("invalid JSON", encoding="utf-8")
    monkeypatch.setattr("app.__main__.Settings", lambda: settings)
    uow = MemoryUow()
    monkeypatch.setattr(
        "app.__main__.build_import", lambda settings, apply: ImportTree(uow, 20000, 32)
    )
    arguments = ["import", "--input", str(input_path), "--collector-report", str(report)]
    if apply:
        arguments.append("--apply")
    assert main(arguments) == 0
    assert uow.committed == apply
    assert f'"mode": "{"apply" if apply else "dry-run"}"' in capsys.readouterr().out


def test_export_uses_configured_depth_limit(monkeypatch, tmp_path, snapshot):
    """Повторная проверка при export соблюдает заданный лимит, а не скрытый default 32."""
    branch = {"external_id": "leaf", "name": "Лист"}
    for level in range(32):
        branch = {"external_id": f"level-{level}", "name": f"Уровень {level}", "children": [branch]}
    path = tmp_path / "deep-input.json"
    write_json(path, {"source": snapshot.source.model_dump(), "complete": False, "nodes": [branch]})
    settings = Settings(
        _env_file=None,
        artifact_dir=tmp_path / "deep-bundle",
        taxonomy_max_depth=64,
        report_file=tmp_path / "reports/report.md",
    )
    monkeypatch.setattr("app.__main__.Settings", lambda: settings)
    assert main(["export", "--input", str(path)]) == 0
