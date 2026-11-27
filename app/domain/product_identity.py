# app/domain/product_identity.py
"""Подтверждённые исходником сведения и ключи идентификации конкретного изделия."""

import re
from decimal import Decimal
from urllib.parse import unquote, urlsplit

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
COMPATIBLE = re.compile(
    r"\b(?:подходит\s+для|совместим\w*\s+с|аналог|для)\s+(?:[-\w]+\s+){0,3}$", re.I
)
MAKER_LABEL = r"\b(?:производитель|изготовитель|manufacturer)\s*[:—-]?\s*"
IDENTITY_LIMIT = 32
CODE = re.compile(r"(?<![\w.-])[\w]+(?:[.,/-][\w]+)*(?![\w.-])")
AXES = {"ширина": "width", "высота": "height", "длина": "length", "глубина": "depth"}


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
    dimension = r"\d+(?:[.,]\d+)?"
    numeric_axes = rf"({dimension}(?:\s*[xх×*]\s*{dimension}){{1,2}})"
    dimensions = re.findall(rf"(?<![\w.]){numeric_axes}\s*(мм|см|mm|cm|м|m)?\b", name, re.I)
    if dimensions:
        values = set()
        for axis_values, unit in dimensions:
            factor = {"см": 10, "cm": 10, "м": 1000, "m": 1000}.get(unit.casefold(), 1)
            values.add(
                "x".join(
                    scalar(str(Decimal(scalar(number)) * factor))
                    for number in re.split(r"\s*[xх×*]\s*", axis_values, flags=re.I)
                )
            )
        features["dimensions"] = sorted(values)
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
        "mounting": (
            r"\bмагнит\w*|\bдин[\s-]*рейк\w*|\bподвесн\w*|\bнакладн\w*|\bвстраива\w*|\bвинт\w*"
        ),
        "material": r"\bмедн\w*|\bалюмини\w*|\bпластик\w*|\bсталь\w*",
        "color": r"\bсер(?:ый|ая|ое)\b|\bч[её]рн\w*|\bбел(?:ый|ая|ое)\b",
    }.items():
        stems = (
            "магнит",
            "дин",
            "подвесн",
            "накладн",
            "встраива",
            "винт",
            "медн",
            "алюмини",
            "пластик",
            "сталь",
            "сер",
            "черн",
            "бел",
        )
        matches = re.findall(pattern, name.casefold().replace("ё", "е"))
        if matches:
            features[key] = sorted(
                {stem for stem in stems if any(m.startswith(stem) for m in matches)}
            )
    if protection := re.findall(r"\bip\s*(\d{2}[a-z]?)\b", name, re.I):
        features["ip"] = sorted({value.casefold() for value in protection})
    if re.search(r"\b(?:выключател\w*|автомат\w*)\b", name, re.I) and (
        ratings := re.findall(r"\b([bcdkбсдк])(\d+(?:[.,]\d+)?)\b", name, re.I)
    ):
        features["trip_rating"] = sorted(
            {
                letter.casefold().translate(str.maketrans("бсдк", "bcdk")) + scalar(number)
                for letter, number in ratings
            }
        )
    for label, axis in AXES.items():
        match = re.search(
            rf"\b{label}\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*(мм|см|mm|cm|м|m)\b", name, re.I
        )
        if match:
            factor = {"см": 10, "cm": 10, "м": 1000, "m": 1000}.get(match[2].casefold(), 1)
            features[axis] = [scalar(str(Decimal(scalar(match[1])) * factor))]
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
    feature_evidence = {field: ["raw_name"] for field in features}
    if technical := normalize_text(fields.get("technical_characteristics")):
        supported = source_features(technical, {})
        for field, value in supported.items():
            features[field] = sorted(set(features.get(field, [])) | set(value))
            feature_evidence.setdefault(field, []).append("raw_technical_characteristics")
    cable = CABLE.search(name)
    if cable and re.search(r"\b(?:кабель|провод)\b", name, re.I):
        mark, cores, section = cable.groups()
        found = claims.get("mark", {}).get("value")
        if not found or text_key(found) == text_key(mark):
            claims["mark"] = {"value": mark, "evidence": "raw_name"}
            features["cores"] = [cores]
            features["section"] = [scalar(section)]
            features["cable_signature"] = [text_key(mark)]
            feature_evidence.update(
                {field: ["raw_name"] for field in ("cores", "section", "cable_signature")}
            )
    if not claims.get("model") and (mpn := claims.get("mpn")) and mpn["evidence"] != "raw_mpn":
        # Старый answer_1 смешивал модель с артикулом. Сохраняем исходное написание.
        if strong_model(text_key(mpn["value"])) and "-" in mpn["value"]:
            claims["model"] = {**mpn}
    url = source_locator(fields.get("origin_url") or fields.get("product_url") or fields.get("url"))
    return {
        "version": 2,
        "claims": claims,
        "features": features,
        "feature_evidence": feature_evidence,
        "name": name,
        "codes": source_codes(name),
        "locators": [url] if url else [],
        "hypotheses": {
            key: value
            for key in IDENTIFIERS
            if key not in claims
            and (
                value := ((details or {}).get(key) or {}).get("value")
                or answer.get(LITERAL_FIELDS.get(key))
            )
        },
    }


def source_locator(value: str | None) -> str | None:
    """Хранит точный адрес карточки с параметрами варианта и без сетевого запроса."""
    try:
        url = urlsplit(value or "")
        if (
            url.scheme in {"http", "https"}
            and url.hostname
            and url.path.strip("/")
            and not url.username
            and not url.password
        ):
            return url._replace(netloc=url.netloc.casefold(), fragment="").geturl()
    except ValueError:
        return None
    return None


def strong_model(value: str) -> bool:
    """Определяет текстовый код исполнения, исключая единицы измерения и простые номера."""
    return (
        len(value) >= 6
        and sum(c.isdecimal() for c in value) >= 2
        and sum(c.isalpha() for c in value) >= 2
        and not re.fullmatch(r"\d+(?:[.,]\d+)?(?:мм|mm|квт|kw|вт|в|kv|w|v|шт)", value)
        and not re.fullmatch(r"\d+(?:[.,]\d+)?(?:[xх]\d+(?:[.,]\d+)?){1,2}(?:мм|mm|см|cm)?", value)
    )


def source_codes(name: str) -> list[str]:
    """Извлекает ограниченное число поисковых токенов без утверждения их роли."""
    codes = {
        v for v in CODE.findall(text_key(name)) if strong_model(v) or re.fullmatch(r"\d{4,18}", v)
    }
    return sorted(codes)[:16]


def identity_retrieval_keys(profile: dict) -> list[str]:
    """Возвращает ключи ограниченной индексной выборки, включая слабые признаки."""
    values = set(profile.get("codes", []))
    values.update(text_key(claim_value(profile, k)) for k in ("mpn", "model", "mark"))
    values.update(text_key(value) for value in profile.get("hypotheses", {}).values())
    keys = {"code:" + fingerprint(value) for value in values if value}
    for locator in profile.get("locators", []):
        keys.add("url:" + fingerprint(locator))
        path = unquote(urlsplit(locator).path).replace("/", " ")
        keys.update("code:" + fingerprint(code) for code in source_codes(path))
    if strict := identity_key(profile):
        keys.add(strict)
    if brand := text_key(claim_value(profile, "brand")):
        if series := text_key(claim_value(profile, "series")):
            keys.add("series:" + fingerprint([brand, series, profile["features"].get("family")]))
    return sorted(keys)


def with_verified_reference(profile: dict, reference: dict) -> dict:
    """Сверяет отдельно проверенный документ с наблюдаемым кодом и параметрами строки."""
    claims = reference["claims"]
    code = text_key(claims["mpn"])
    name = profile.get("name", "")
    url = source_locator(reference.get("document"))
    if not url or SELLER_CODE.search(name):
        return profile
    in_title = literal(code, name)
    in_path = any(
        code in source_codes(unquote(urlsplit(locator).path).replace("/", " "))
        for locator in profile.get("locators", [])
    )
    words = reference.get("type_terms", [])
    if not (in_title or in_path) or (words and not any(w in text_key(name) for w in words)):
        return profile
    features = reference.get("features", {})
    if not all(profile["features"].get(field) == value for field, value in features.items()):
        return profile
    same_document = url.rstrip("/") in {v.rstrip("/") for v in profile.get("locators", [])}
    model = claims.get("model")
    if not in_title and not (
        (features and literal(claims["brand"], name))
        or (same_document and model and literal(model, name))
    ):
        return profile
    verified = {
        "claims": {
            k: {"value": v, "evidence": "verified_document", "reference": url}
            for k, v in claims.items()
        },
        "features": {},
    }
    observed = refine_legacy_roles(profile, verified)
    if identity_conflicts(observed, verified):
        return profile
    return {**observed, "claims": {**observed["claims"], **verified["claims"]}}


def refine_legacy_roles(profile: dict, verified: dict) -> dict:
    """Разрешает уточнение роли строкового значения только проверенным документом."""
    claims = dict(profile["claims"])
    for field, alternative in (("mpn", "model"), ("brand", "series")):
        value = claims.get(field, {})
        other = verified["claims"].get(alternative, {})
        if (
            value.get("evidence") == "legacy_raw_name"
            and other.get("evidence") == "verified_document"
            and text_key(value["value"]) == text_key(other["value"])
        ):
            claims.pop(field)
    return {**profile, "claims": claims}


def claim_value(profile: dict, field: str) -> str | None:
    """Возвращает только значение, прошедшее проверку об исходник."""
    return profile["claims"].get(field, {}).get("value")


def canonical_brand(profile: dict, aliases: dict[str, str]) -> dict:
    """Применяет только утверждённый алиас и сохраняет исходное написание."""
    original = claim_value(profile, "brand")
    if name := aliases.get(text_key(original)):
        brand = {**profile["claims"]["brand"], "value": name, "source_value": original}
        return {**profile, "claims": {**profile["claims"], "brand": brand}}
    return profile


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
        if field in {"unit", "quantity"}:
            continue
        other = right["features"].get(field)
        if other and value != other:
            mismatches.append(field)
    return mismatches


def identity_proof(left: dict, right: dict) -> str | None:
    """Отделяет подтверждённую связь от совпадения поисковых токенов."""
    if identity_conflicts(left, right):
        return None
    if (key := identity_key(left)) and key == identity_key(right):
        return "issuer_mpn" if key.startswith("mpn:") else "full_cable_signature"
    maker = text_key(claim_value(left, "manufacturer"))
    left_mpn = text_key(claim_value(left, "mpn"))
    if (
        maker
        and maker == text_key(claim_value(right, "manufacturer"))
        and left_mpn == text_key(claim_value(right, "mpn"))
        and all((identity_key(p) or "").startswith("mpn:") for p in (left, right))
    ):
        return "manufacturer_mpn"
    for known, given in ((left, right), (right, left)):
        scope = claim_value(known, "brand") or claim_value(known, "manufacturer")
        code = text_key(claim_value(known, "mpn"))
        if not scope or not code or not (identity_key(known) or strong_model(code)):
            continue
        given_name = given.get("name", "")
        if (
            literal(scope, given_name)
            and literal(code, given_name)
            and not SELLER_CODE.search(given_name)
        ):
            return "literal_issuer_and_code"
        for locator in set(known.get("locators", [])) & set(given.get("locators", [])):
            if literal(code, known.get("name", "")) and literal(code, given_name) and locator:
                return "same_source_and_code"
    lm, rm = (text_key(claim_value(p, "model")) for p in (left, right))
    brand = text_key(claim_value(left, "brand"))
    same_scope = (brand and brand == text_key(claim_value(right, "brand"))) or (
        maker and maker == text_key(claim_value(right, "manufacturer"))
    )
    if strong_model(lm) and lm == rm and same_scope:
        return "issuer_full_model"
    if set(left.get("locators", [])) & set(right.get("locators", [])):
        if (
            strong_model(lm)
            and lm == rm
            and all(
                literal(lm, p.get("name", "")) and not SELLER_CODE.search(p.get("name", ""))
                for p in (left, right)
            )
        ):
            return "same_source_and_variant"
    return None


def decide_identity(profile: dict, candidates: list[tuple], *, overflow: bool = False) -> dict:
    """Принимает решение об идентичности с явным отказом при неоднозначности."""
    evidence = []
    proven = []
    for pid, candidate in candidates:
        conflicts = identity_conflicts(profile, candidate)
        proof = identity_proof(profile, candidate) if not conflicts else None
        evidence.append({"product_id": str(pid), "conflicts": conflicts, "proof": proof})
        if proof:
            proven.append(pid)
    target = proven[0] if len(proven) == 1 and not overflow else None
    decision = "same" if target else "different"
    if not target and (overflow or any(not row["conflicts"] for row in evidence)):
        decision = "uncertain"
    if overflow:
        reason = "candidate_limit"
    elif len(proven) > 1:
        reason = "multiple_proven"
    elif target:
        reason = next(row["proof"] for row in evidence if row["product_id"] == str(target))
    elif decision == "uncertain":
        reason = "insufficient_evidence"
    else:
        reason = "conflicting_facts" if evidence else "no_candidates"
    return {
        "decision": decision,
        "product_id": target,
        "candidates": evidence,
        "reason": reason,
    }


def accumulate_profile(left: dict, right: dict) -> dict:
    """Объединяет совместимые сведения без замены ранее сохранённых фактов."""
    return {
        "version": 2,
        "claims": {**right["claims"], **left["claims"]},
        "features": {**right["features"], **left["features"]},
        "feature_evidence": {
            **right.get("feature_evidence", {}),
            **left.get("feature_evidence", {}),
        },
        "name": left.get("name") or right.get("name", ""),
        "codes": sorted(set(left.get("codes", [])) | set(right.get("codes", [])))[:16],
        "locators": sorted(set(left.get("locators", [])) | set(right.get("locators", [])))[:16],
        "hypotheses": {**right.get("hypotheses", {}), **left.get("hypotheses", {})},
    }


def searchable_mpn(profile: dict) -> str | None:
    """Отдельно вычисляет старый поисковый ключ артикула для карточки."""
    return normalized_key(claim_value(profile, "mpn")) or None
