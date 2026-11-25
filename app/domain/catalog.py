# app/domain/catalog.py
"""Независимые DTO и правила нормализации номенклатуры, доказательств и статусов этапа 02."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

FIELDS = (
    "name",
    "source_key",
    "brand",
    "manufacturer",
    "mpn",
    "unit",
    "quantity",
    "price",
    "currency",
    "price_date",
    "seller",
    "seller_inn",
    "product_url",
    "category_path",
)
TERMINAL_ITEMS = {
    "assigned",
    "needs_review",
    "taxonomy_gap",
    "not_product",
    "invalid_record",
    "error",
}
TERMINAL_JOBS = {"completed", "completed_with_issues", "failed", "canceled"}
NORMALIZER_VERSION = "catalog_normalizer_v1"


def item_counters(items: list[dict]) -> dict:
    """Считает реальные terminal позиции одинаково для job и каждого SourceRun файла."""
    result = {
        "total_count": len(items),
        "processed_count": sum(i["status"] in TERMINAL_ITEMS for i in items),
    }
    for field_name, statuses in {
        "assigned_count": {"assigned"},
        "needs_review_count": {"needs_review"},
        "taxonomy_gap_count": {"taxonomy_gap"},
        "error_count": {"error", "invalid_record"},
    }.items():
        result[field_name] = sum(i["status"] in statuses for i in items)
    return result


class CatalogError(ValueError):
    """Ожидаемая ошибка входа, состояния или оптимистической блокировки."""


class ConflictError(CatalogError):
    """Состояние изменилось после просмотра либо ключ идемпотентности использован иначе."""


class DependencyUnavailable(RuntimeError):
    """Обязательная внешняя зависимость временно недоступна; raw не удаляется."""


class InvalidModelOutput(CatalogError):
    """Невалидный финальный ответ сохраняется для аудита, но не принимается решением."""

    def __init__(self, message: str, raw_response: object):
        """Отделяет безопасную диагностику от исходного model output."""
        super().__init__(message)
        self.raw_response = (
            raw_response
            if isinstance(raw_response, str) or raw_response is None
            else json.dumps(raw_response, ensure_ascii=False)
        )


@dataclass
class InputRow:
    """Исходная строка с отдельно отображёнными canonical полями и trace locator."""

    position: int
    row_number: int
    sheet_name: str | None
    raw: dict
    fields: dict
    issues: list[dict] = field(default_factory=list)


@dataclass
class ParsedCatalog:
    """Результат форматного адаптера для общего use-case, без SQL и inference."""

    profile: str
    source_name: str
    rows: list[InputRow]
    columns: list[str]
    sheets: list[str] = field(default_factory=list)
    mapping: dict = field(default_factory=dict)
    requires_mapping: bool = False
    warnings: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def json_value(value: Any) -> Any:
    """Приводит технические значения к JSON без float-конверсии Decimal и потери строк."""
    if isinstance(value, (date, datetime, Decimal, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def fingerprint(value: Any) -> str:
    """Вычисляет воспроизводимый SHA payload/версии с сохранением Unicode и порядка объектов."""
    raw = json.dumps(json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_text(value: object) -> str | None:
    """Очищает пробелы только в derived-слое, не меняя raw payload."""
    if value is None:
        return None
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()) or None


def normalized_key(value: object) -> str:
    """Нормализует ключ поиска без недоказанной транслитерации артикулов разных алфавитов."""
    return re.sub(r"[\s\-]+", "", normalize_text(value) or "").casefold()


def supported_candidate(candidate: str | None, fields: dict) -> str | None:
    """Требует буквального подтверждения model candidate в исходном описании/полях."""
    if not candidate:
        return None
    normalized = normalized_key(candidate)
    facts = [normalized_key(fields.get(key)) for key in ("name", "brand", "manufacturer", "mpn")]
    return candidate if normalized and any(normalized in value for value in facts) else None


def is_bare_identifier(fields: dict) -> bool:
    """Отличает одиночный буквенно-цифровой артикул без описания от названия изделия."""
    name = fields.get("name") or ""
    return bool(
        re.fullmatch(r"[\w./-]{2,80}", name)
        and re.search(r"\d", name)
        and re.search(r"[A-Za-zА-Яа-я]", name)
    )


def identity_features(name: str | None) -> dict:
    """Извлекает только явно написанные признаки варианта для консервативной проверки конфликта."""
    text = name or ""
    patterns = {
        "current": r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:[AА]|ампер\w*)(?=\W|$)",
        "poles": r"(?<!\w)([1-4])\s*[PР](?=\W|$)",
        "voltage": r"(?<!\d)(\d{2,4})\s*[VВ](?=\W|$)",
        "dimensions": (
            r"(?<!\d)(\d+(?:[.,]\d+)?\s*[xх×*]\s*\d+(?:[.,]\d+)?"
            r"(?:\s*[xх×*]\s*\d+(?:[.,]\d+)?)?)"
        ),
    }
    result = {}
    for key, pattern in patterns.items():
        values = re.findall(pattern, text, flags=re.I)
        if values:
            result[key] = sorted(
                set(
                    re.sub(r"[\s]", "", v)
                    .replace(",", ".")
                    .replace("х", "x")
                    .replace("×", "x")
                    .replace("*", "x")
                    for v in values
                )
            )
    return result


def query_attributes(answer: dict) -> str:
    """Оставляет признаки вида изделия; идентификаторы, цены и продавцы исключаются."""
    allowed = (
        "тип",
        "вид",
        "материал",
        "назначение",
        "исполнение",
        "принцип",
        "форма",
        "функция",
        "напряжение",
        "питание",
        "изоляция",
        "полюс",
        "сечение",
    )
    denied = ("mpn", "артикул", "sku", "gtin", "ean", "цена", "продавец", "бренд", "модель")
    attributes = [
        a
        for a in answer.get("attributes", [])
        if any(word in a["name"].casefold() for word in allowed)
        and not any(word in a["name"].casefold() for word in denied)
    ]
    return "; ".join(f"{a['name']}: {a['value']} {a.get('unit') or ''}" for a in attributes)


def issue(code: str, field_name: str, raw: object, message: str, severity: str = "warning") -> dict:
    """Создаёт единый диагностический DTO с сохранением исходного значения и стадии."""
    return {
        "code": code,
        "field": field_name,
        "raw_value": json_value(raw),
        "stage": "normalization",
        "severity": severity,
        "message": message,
        "evidence_ids": [f"raw_{field_name}"],
        "resolution_status": "open",
    }


def decimal_value(value: object, field_name: str, issues: list[dict]) -> str | None:
    """Разбирает десятичную сумму; повреждённое число не исправляет догадкой."""
    text = normalize_text(value)
    if text is None:
        return None
    text = text.replace(" ", "").replace(",", ".")
    try:
        if not re.fullmatch(r"\d+(?:\.\d+)?", text):
            raise InvalidOperation
        parsed = Decimal(text)
        if (
            not parsed.is_finite()
            or parsed < 0
            or len(parsed.as_tuple().digits) > 20
            or parsed.as_tuple().exponent < -6
            or parsed.adjusted() + 1 > (12 if field_name == "quantity" else 14)
        ):
            raise InvalidOperation
        return format(parsed, "f")
    except (InvalidOperation, ValueError):
        issues.append(
            issue(
                f"invalid_{field_name}",
                field_name,
                value,
                "Некорректное десятичное значение",
                "error",
            )
        )
        return None


def normalize_row(row: InputRow) -> dict:
    """Нормализует имя/единицу/дату/деньги, сохраняя неизвестное неизвестным."""
    fields = {key: normalize_text(row.fields.get(key)) for key in FIELDS if key != "category_path"}
    fields["category_path"] = row.fields.get("category_path")
    issues = list(row.issues)
    if row.fields.get("name") is not None and not isinstance(row.fields["name"], str):
        fields["name"] = None
        issues.append(
            issue(
                "invalid_name",
                "name",
                row.fields["name"],
                "Наименование должно быть строкой",
                "error",
            )
        )
    path = fields["category_path"]
    if path is not None and (
        not isinstance(path, list) or not all(isinstance(value, str) for value in path)
    ):
        fields["category_path"] = None
        issues.append(
            issue(
                "invalid_category_path",
                "category_path",
                path,
                "Путь категории должен быть массивом строк",
            )
        )
    if not fields["name"]:
        issues.append(
            issue(
                "missing_name", "name", row.fields.get("name"), "Наименование отсутствует", "error"
            )
        )
    unit = fields["unit"]
    if unit is None:
        issues.append(issue("missing_unit", "unit", None, "Единица не указана"))
    elif re.match(r"\[B@|^=|<script", unit, re.I) or len(unit) > 32:
        issues.append(issue("invalid_unit", "unit", unit, "Единица повреждена", "error"))
        fields["unit"] = None
    fields["price"] = decimal_value(row.fields.get("price"), "price", issues)
    fields["quantity"] = decimal_value(row.fields.get("quantity"), "quantity", issues)
    if fields["currency"]:
        currency = fields["currency"].upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            issues.append(
                issue("invalid_currency", "currency", fields["currency"], "Неизвестный код валюты")
            )
            currency = None
        fields["currency"] = currency
    if fields["price_date"]:
        raw_date = fields["price_date"]
        try:
            parsed_date = (
                datetime.strptime(raw_date, "%d.%m.%Y").date()
                if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", raw_date)
                else date.fromisoformat(raw_date[:10])
            )
            fields["price_date"] = parsed_date.isoformat()
        except ValueError:
            issues.append(
                issue("invalid_date", "price_date", raw_date, "Дата источника не распознана")
            )
            fields["price_date"] = None
    if fields["product_url"] and not re.match(r"^https?://[^\s]+$", fields["product_url"]):
        issues.append(
            issue(
                "invalid_url",
                "product_url",
                fields["product_url"],
                "Ссылка не распознана; сеть не проверялась",
            )
        )
    return {
        "fields": fields,
        "issues": issues,
        "normalizer_version": NORMALIZER_VERSION,
        "status": "invalid" if not fields["name"] else "needs_review" if issues else "parsed",
    }


def evidence_bundle(record_id: str, normalized: dict, locator: dict, max_chars: int) -> dict:
    """Формирует ограниченный пакет фактов без продавца, ИНН, адресов, цен и секретов URL."""
    fields = normalized["fields"]
    selected = {
        key: fields.get(key)
        for key in ("name", "brand", "mpn", "manufacturer", "unit", "category_path")
    }
    evidence = [
        {
            "evidence_id": f"raw_{key}",
            "source_kind": "input_row",
            "locator": locator,
            "text": str(value)[:max_chars],
            "trust_level": "unverified",
        }
        for key, value in selected.items()
        if value is not None
    ]
    result = {
        "record_id": record_id,
        "raw_name": selected.get("name"),
        "raw_mpn": selected.get("mpn"),
        "raw_brand": selected.get("brand"),
        "source_record": selected,
        "normalized_fields": selected,
        "evidence": evidence,
    }
    if len(json.dumps(result, ensure_ascii=False)) > max_chars:
        raise CatalogError("Пакет evidence превышает лимит поля")
    return result
