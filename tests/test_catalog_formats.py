# tests/test_catalog_formats.py
"""Unit/regression входных адаптеров, нормализации, evidence и строгого inference."""

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.domain.catalog import (
    CatalogError,
    InputRow,
    evidence_bundle,
    normalize_row,
    normalized_key,
)
from app.infrastructure.catalog_formats import CatalogFileParser
from app.infrastructure.inference import SharedInference
from app.settings import Settings


def parser() -> CatalogFileParser:
    """Создаёт offline parser без подключений к PostgreSQL/shared."""
    return CatalogFileParser(Settings(_env_file=None, inference_mode="mock"))


def test_normalization_does_not_guess_or_mutate():
    """Java-unit/повреждённая цена остаются в raw; продавец не становится брендом."""
    raw = {
        "name": "  Кабель   силовой ",
        "unit": "[B@123",
        "price": "12a",
        "seller": "ООО",
        "price_date": "30.11.2024",
    }
    result = normalize_row(InputRow(0, 1, None, raw.copy(), raw))
    assert result["fields"]["name"] == "Кабель силовой"
    assert result["fields"]["unit"] is None and result["fields"]["price"] is None
    assert result["fields"]["currency"] is None and result["fields"]["brand"] is None
    assert result["fields"]["price_date"] == "2024-11-30"
    assert raw["price"] == "12a"
    assert {i["code"] for i in result["issues"]} >= {"invalid_unit", "invalid_price"}
    bundle = evidence_bundle("record", result, {"row": 1}, 16000)
    text = json.dumps(bundle, ensure_ascii=False)
    assert "ООО" not in text and "2024" not in text and '"price"' not in text
    assert normalized_key("А-123") != normalized_key("A-123")


def test_canonical_and_generic_mapping(tmp_path):
    """Canonical проходит автоматически, неизвестные колонки требуют явного mapping."""
    path = tmp_path / "input.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "synthetic",
                "items": [{"name": "Кабель тестовый", "price": "12.50"}, {"price": "bad"}],
            }
        ),
        encoding="utf-8",
    )
    parsed = parser().parse(path, path.name)
    assert not parsed.requires_mapping and len(parsed.rows) == 2
    assert normalize_row(parsed.rows[1])["status"] == "invalid"
    path.write_text('[{"Описание":"Кабель тестовый"}]', encoding="utf-8")
    assert parser().parse(path, path.name).requires_mapping
    parsed = parser().parse(path, path.name, {"fields": {"name": "Описание"}, "confirmed": True})
    assert not parsed.requires_mapping and parsed.rows[0].fields["name"] == "Кабель тестовый"


def test_xml_xxe_and_row_validation(tmp_path):
    """DTD/XXE отвергается; некорректная строка безопасного XML сохраняет issue."""
    path = tmp_path / "input.xml"
    path.write_text(
        '<!DOCTYPE catalog [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<catalog schema_version="1" source="test"><item><name>&x;</name></item></catalog>',
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="XML"):
        parser().parse(path, path.name)
    path.write_text(
        '<catalog schema_version="1" source="test"><item><name>Кабель тестовый</name>'
        "<price>12.50</price></item></catalog>",
        encoding="utf-8",
    )
    assert parser().parse(path, path.name).rows[0].fields["price"] == "12.50"


def test_csv_encoding_quoting_and_xlsx_formulas(tmp_path):
    """CP1251 выбирается явно, quoting работает, формулы XLSX не исполняются."""
    path = tmp_path / "input.csv"
    path.write_bytes('name;price\n"Кабель; тестовый";"12,50"\n'.encode("cp1251"))
    with pytest.raises(CatalogError, match="encoding"):
        parser().parse(path, path.name)
    parsed = parser().parse(path, path.name, {"encoding": "cp1251"})
    assert parsed.rows[0].fields["name"] == "Кабель; тестовый"
    assert normalize_row(parsed.rows[0])["fields"]["price"] == "12.50"
    book = Workbook()
    book.active.append(["name", "price"])
    book.active.append(["Кабель тестовый", "=1+2"])
    path = tmp_path / "input.xlsx"
    book.save(path)
    book.close()
    parsed = parser().parse(path, path.name)
    assert parsed.rows[0].raw["_formulas"]["B2"] == "=1+2"
    assert parsed.rows[0].fields["price"] is None
    assert parsed.rows[0].issues[0]["code"] == "formula_without_cached_value"


def test_inference_rejects_fabricated_references(monkeypatch):
    """Даже mock не может вернуть вымышленный evidence или UUID категории."""
    client = SharedInference(Settings(_env_file=None, inference_mode="mock"))
    normalized = normalize_row(InputRow(0, 1, None, {}, {"name": "Кабель тестовый"}))
    values = {"evidence_bundle_json": evidence_bundle("id", normalized, {}, 16000)}
    result = client.answer(1, values)
    assert result["mode"] == "mock"
    answer = result["answer_json"]
    answer["evidence_ids"] = ["invented"]
    monkeypatch.setattr(client, "mock_answer", lambda step, values: answer)
    with pytest.raises(CatalogError, match="evidence"):
        client.answer(1, values)
    assert len(client.embed(["Кабель"])[0]) == 64


def test_real_aitim_profile_if_available():
    """Настоящий архив не считается gold; проверяются только формат и повреждения unit."""
    path = Path("examples/aitim_200_raw.json")
    if not path.exists():
        pytest.skip("AITIM fixture недоступна")
    parsed = parser().parse(path, path.name)
    assert parsed.profile == "aitim_archive_v1" and len(parsed.rows) == 200
    normalized = [normalize_row(row) for row in parsed.rows]
    assert sum(any(i["code"] == "missing_unit" for i in row["issues"]) for row in normalized) == 198
    assert sum(any(i["code"] == "invalid_unit" for i in row["issues"]) for row in normalized) == 2
