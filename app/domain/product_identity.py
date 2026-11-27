# app/domain/product_identity.py
"""Подтверждённые исходником сведения и ключи идентификации конкретного изделия."""

import re
from decimal import Decimal

from app.domain.catalog import fingerprint, identity_features, normalize_text, normalized_key

IDENTIFIERS = ("brand", "manufacturer", "mpn", "model", "mark", "series")
LITERAL_FIELDS = {
    "brand": "brand_candidate",
    "manufacturer": "manufacturer_candidate",
    "mpn": "mpn_candidate",
}
TECHNICAL = {
    "voltage": r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(кв|kv|в|v)(?!\w)",
    "power": r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(квт|kw|вт|w)(?!\w)",
    "diameter": r"(?:[⌀øф]|диаметр\s*)(\d+(?:[.,]\d+)?)\s*(?:мм|mm)?",
    "section": r"(?:сечение\s*)(\d+(?:[.,]\d+)?)\s*(?:мм2|mm2)?",
    "cores": r"(?<!\w)(\d+)\s*(?:жил(?:а|ы)?)(?!\w)",
}
CABLE = re.compile(
    r"(?<!\w)(аввг[\w()\-]*|ввг[\w()\-]*|пвс[\w()\-]*|шввп[\w()\-]*|"
    r"кг[\w()\-]*|nym[\w()\-]*)\s+"
    r"(\d+)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)(?![\w.,\-]|\s*[xх×*+])",
    flags=re.I,
)
SELLER_CODE = re.compile(r"(?:код\s+(?:товара|продавца)|sku|артикул\s+продавца)\b", re.I)
COMPATIBLE = re.compile(r"\b(?:для|подходит\s+для|совместим\w*\s+с|аналог)\s*$", re.I)
MAKER_LABEL = r"\b(?:производитель|изготовитель|manufacturer)\s*[:—-]?\s*"


def text_key(value: str | None) -> str:
    """Сравнивает регистр и пробелы, сохраняя разделители и исходный алфавит."""
    return (normalize_text(value) or "").casefold()


def literal(value: str, source: str) -> bool:
    """Проверяет полное вхождение значения с границами и контекстом изделия."""
    match = re.search(
        r"(?<![\w/+.-])" + re.escape(text_key(value)) + r"(?![\w/+-]|[.,]\w)",
        text_key(source),
    )
    return bool(match and not COMPATIBLE.search(text_key(source)[: match.start()]))


def scalar(value: str) -> str:
    """Нормализует десятичное написание технического числа без округления."""
    try:
        return format(Decimal(value.replace(",", ".")).normalize(), "f")
    except ArithmeticError:
        return value


def source_features(name: str, answer: dict) -> dict[str, list[str]]:
    """Извлекает различия исполнения и допускает только буквально указанные атрибуты."""
    features = identity_features(name.casefold())
    family = re.search(
        r"^\s*(светильник|кабель|провод|выключатель|розетка|труба|клапан)\b", name, re.I
    )
    if family:
        features["family"] = [family[1].casefold()]
    for key, pattern in TECHNICAL.items():
        values = re.findall(pattern, name, flags=re.I)
        if key in {"voltage", "power"}:
            parsed = set()
            for number, unit in values:
                value = scalar(number)
                if unit.casefold().startswith(("к", "k")):
                    value = scalar(str(Decimal(value) * 1000))
                parsed.add(value)
        else:
            parsed = {scalar(number) for number in values}
        if parsed:
            features[key] = sorted(parsed)
    for key, pattern in {
        "mounting": r"\bмагнит\w*|\bдин[\s-]*рейк\w*|\bподвесн\w*|\bнакладн\w*|\bвстраива\w*",
        "material": r"\bмедн\w*|\bалюмини\w*|\bпластик\w*|\bсталь\w*",
    }.items():
        stems = (
            "магнит",
            "дин",
            "подвесн",
            "накладн",
            "встраива",
            "медн",
            "алюмини",
            "пластик",
            "сталь",
        )
        matches = re.findall(pattern, name.casefold())
        if matches:
            features[key] = sorted(
                {stem for stem in stems if any(m.startswith(stem) for m in matches)}
            )
    attributes = answer.get("attributes") or []
    for attribute in attributes:
        value, label = attribute.get("value"), text_key(attribute.get("name"))
        if (
            value
            and label
            and "raw_name" in attribute.get("evidence_ids", [])
            and literal(value, name)
        ):
            # В контракте VLM могут быть синонимы названий и разные единицы.
            # Только строго одноимённые дополнительные параметры служат проверкой конфликта.
            if label in {
                "цвет",
                "класс защиты",
                "степень защиты",
                "исполнение",
                "форма",
                "стандарт",
            }:
                features[f"attribute:{label}"] = [text_key(value)]
    return features


def identity_profile(fields: dict, answer: dict | None = None) -> dict:
    """Отделяет доказуемые атрибуты изделия от гипотез и исходного кода продавца."""
    answer = answer or {}
    name = normalize_text(fields.get("name")) or ""
    claims = {}
    recognized = (
        answer.get("status") == "recognized" and answer.get("record_kind") == "product_candidate"
    )
    details = answer.get("identifiers")
    for kind in IDENTIFIERS:
        explicit = normalize_text(fields.get(kind))
        candidate = (details or {}).get(kind) or {}
        value = normalize_text(candidate.get("value"))
        if explicit:
            claims[kind] = {"value": explicit, "evidence": f"raw_{kind}"}
        elif recognized and value and candidate.get("origin") == "source":
            if "raw_name" in (candidate.get("evidence_ids") or []) and literal(value, name):
                claims[kind] = {"value": value, "evidence": "raw_name"}
        elif recognized and details is None and kind in LITERAL_FIELDS:
            legacy = normalize_text(answer.get(LITERAL_FIELDS[kind]))
            if legacy and literal(legacy, name):
                claims[kind] = {"value": legacy, "evidence": "legacy_raw_name"}
    manufacturer = claims.get("manufacturer")
    if (
        manufacturer
        and manufacturer["evidence"] != "raw_manufacturer"
        and not re.search(
            MAKER_LABEL + re.escape(text_key(manufacturer["value"])) + r"(?![\w/\-])",
            text_key(name),
        )
    ):
        del claims["manufacturer"]
    if "mpn" in claims and (SELLER_CODE.search(name) and claims["mpn"]["evidence"] != "raw_mpn"):
        del claims["mpn"]
    if recognized and details is None and "series" not in claims:
        for attribute in answer.get("attributes", []):
            if (
                text_key(attribute.get("name")) in {"серия", "серия/линейка", "линейка"}
                and (value := attribute.get("value"))
                and literal(value, name)
                and "raw_name" in attribute.get("evidence_ids", [])
            ):
                claims["series"] = {"value": value, "evidence": "legacy_raw_name"}
                break
    features = source_features(name, answer)
    cable = CABLE.search(name)
    if cable and re.search(r"\b(?:кабель|провод)\b", name, re.I):
        mark, cores, section = cable.groups()
        found = claims.get("mark", {}).get("value")
        if not found or text_key(found) == text_key(mark):
            claims["mark"] = {"value": mark, "evidence": "raw_name"}
            features["cores"] = [cores]
            features["section"] = [scalar(section)]
            features["cable_signature"] = [text_key(mark)]
    for key in ("unit", "quantity"):
        if value := text_key(fields.get(key)):
            features[key] = [value]
    return {"claims": claims, "features": features}


def claim_value(profile: dict, field: str) -> str | None:
    """Возвращает только значение, прошедшее проверку об исходник."""
    return profile["claims"].get(field, {}).get("value")


def identity_key(profile: dict) -> str | None:
    """Строит строгий ключ MPN либо полный ключ поддерживаемого исполнения кабеля."""
    scope = "brand" if claim_value(profile, "brand") else "manufacturer"
    issuer = text_key(claim_value(profile, scope))
    mpn = text_key(claim_value(profile, "mpn"))
    if mpn:
        explicit = profile["claims"]["mpn"]["evidence"] == "raw_mpn"
        if issuer and 3 <= len(mpn) <= 256 and mpn not in {"нет", "unknown", "none", "n/a"}:
            if explicit or any(char.isdecimal() for char in mpn):
                return "mpn:" + fingerprint([scope, issuer, mpn])
        return None
    mark = text_key(claim_value(profile, "mark"))
    features = profile["features"]
    axes = ("cores", "section", "voltage")
    if (
        mark
        and issuer
        and features.get("cable_signature") == [mark]
        and features.get("family") in (["кабель"], ["провод"])
        and all(len(features.get(k, [])) == 1 for k in axes)
    ):
        values = [features[k][0] for k in axes]
        return "cable:" + fingerprint([scope, issuer, mark, values])
    return None


def identity_conflicts(left: dict, right: dict) -> list[str]:
    """Находит отличающиеся известные маркеры и технические исполнения."""
    mismatches = [
        key
        for key in IDENTIFIERS
        if claim_value(left, key)
        and claim_value(right, key)
        and text_key(claim_value(left, key)) != text_key(claim_value(right, key))
    ]
    for field, value in left["features"].items():
        other = right["features"].get(field)
        if other and value != other:
            mismatches.append(field)
    return mismatches


def accumulate_profile(left: dict, right: dict) -> dict:
    """Объединяет совместимые сведения без замены ранее сохранённых фактов."""
    return {
        "claims": {**right["claims"], **left["claims"]},
        "features": {**right["features"], **left["features"]},
    }


def searchable_mpn(profile: dict) -> str | None:
    """Отдельно вычисляет старый поисковый ключ артикула для карточки."""
    return normalized_key(claim_value(profile, "mpn")) or None
