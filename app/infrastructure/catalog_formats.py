# app/infrastructure/catalog_formats.py
"""Ограниченные адаптеры JSON/XML/XLSX/CSV с сохранением исходных строк и явным mapping."""

import csv
import io
import json
import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from jsonschema import Draft202012Validator, FormatChecker
from lxml import etree
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.domain.catalog import FIELDS, CatalogError, InputRow, ParsedCatalog, issue, json_value
from app.settings import Settings

ALIASES = {
    "наименование": "name",
    "название": "name",
    "номенклатура": "name",
    "бренд": "brand",
    "производитель": "manufacturer",
    "артикул производителя": "mpn",
    "модель": "model",
    "марка": "mark",
    "серия": "series",
    "код продавца": "seller_sku",
    "технические характеристики": "technical_characteristics",
    "артикул": "source_key",
    "ед. изм.": "unit",
    "единица": "unit",
    "цена": "price",
    "валюта": "currency",
    "количество": "quantity",
    "дата": "price_date",
    "продавец": "seller",
    "инн": "seller_inn",
    "ссылка": "product_url",
    "категория": "category_path",
}


def unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    """Отклоняет дубли JSON-ключей, чтобы parser не выбирал молча последнее значение."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("JSON содержит повтор ключа")
        result[key] = value
    return result


def reject_json_constant(value: str):
    """NaN/Infinity не входят в стандарт JSON."""
    raise CatalogError("JSON содержит недопустимую числовую константу")


class CatalogFileParser:
    """Один набор форматных адаптеров для CLI, HTTP preview и фонового ingestion."""

    def __init__(self, settings: Settings) -> None:
        """Получает все лимиты и локальные схемы через composition root."""
        self.settings = settings
        self.schema = json.loads(
            Path("docs/contracts/catalog_items_v1.schema.json").read_text(encoding="utf-8")
        )
        self.row_schema = {**self.schema["$defs"]["item"], "$defs": self.schema["$defs"]}
        self.validator = Draft202012Validator(self.row_schema, format_checker=FormatChecker())

    def parse(self, path: Path, filename: str, options: dict | None = None) -> ParsedCatalog:
        """Проверяет сигнатуру/лимиты, читает выбранные строки и накладывает отдельное mapping."""
        options = options or {}
        if not isinstance(options, dict):
            raise CatalogError("Параметры формата должны быть объектом")
        fields = options.get("fields", {})
        if not isinstance(fields, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in fields.items()
        ):
            raise CatalogError("Mapping должен сопоставлять строковые названия колонок")
        if "source" in options and (
            not isinstance(options["source"], str)
            or not options["source"].strip()
            or len(options["source"]) > 128
        ):
            raise CatalogError("Источник должен быть непустой строкой до 128 символов")
        if "sheets" in options and (
            not isinstance(options["sheets"], list)
            or not all(isinstance(v, str) for v in options["sheets"])
        ):
            raise CatalogError("Выбор листов должен быть массивом названий")
        if "header_row" in options and (
            not isinstance(options["header_row"], int) or not 1 <= options["header_row"] <= 1000
        ):
            raise CatalogError("Строка заголовков должна быть числом от 1 до 1000")
        extension = Path(filename).suffix.lower()
        if extension not in {".json", ".xml", ".xlsx", ".csv"}:
            raise CatalogError("Поддерживаются JSON, XML, XLSX и CSV; XLS/XLSM/SVC не принимаются")
        if path.stat().st_size > self.settings.upload_max_bytes:
            raise CatalogError("Превышен лимит размера файла")
        raw = path.read_bytes()
        if not raw:
            raise CatalogError("Пустой файл")
        if extension == ".json":
            parsed = self._json(raw, options)
        elif extension == ".xml":
            parsed = self._xml(raw, options)
        elif extension == ".csv":
            parsed = self._csv(raw, options)
        else:
            parsed = self._xlsx(path, raw, options)
        if len(parsed.rows) > self.settings.upload_max_rows:
            raise CatalogError("Превышен лимит строк")
        if not parsed.rows:
            raise CatalogError("В файле нет строк данных")
        for row in parsed.rows:
            for value in row.raw.values():
                if len(str(value)) > self.settings.upload_max_field_chars:
                    raise CatalogError("Поле превышает лимит длины")
        if (
            parsed.source_name == "user_upload"
            and parsed.profile.endswith("mapped")
            and not options.get("source")
        ):
            parsed.source_name = "user_upload:" + Path(filename.replace("\\", "/")).name
        return parsed

    def _map(self, raw: dict, mapping: dict) -> dict:
        """Сопоставляет поля, не удаляя из raw несопоставленные колонки."""
        result = {key: raw.get(mapping.get(key, key)) for key in FIELDS}
        path = result["category_path"]
        if isinstance(path, str):
            result["category_path"] = [
                part.strip() for part in path.split(" > ") if part.strip()
            ] or None
        return result

    def _mapping(self, columns: list[str], options: dict) -> tuple[dict, bool]:
        """Предлагает только точные canonical/известные заголовки; неизвестное требует выбора."""
        mapping = {key: key for key in FIELDS if key in columns}
        for column in columns:
            canonical = ALIASES.get(column.strip().casefold())
            if canonical and canonical not in mapping:
                mapping[canonical] = column
        mapping.update(options.get("fields", {}))
        if any(k not in FIELDS or v not in columns for k, v in mapping.items()):
            raise CatalogError("Mapping ссылается на неизвестную колонку или поле")
        return mapping, "name" not in mapping

    def _json(self, raw: bytes, options: dict) -> ParsedCatalog:
        """Распознаёт исходный AITIM, строгий canonical JSON и явно сопоставленный массив."""
        try:
            document = json.loads(
                raw, object_pairs_hook=unique_json_object, parse_constant=reject_json_constant
            )
        except (ValueError, UnicodeError, RecursionError) as error:
            raise CatalogError("Некорректный JSON/UTF-8") from error
        profile = options.get("profile", "auto")
        if (
            isinstance(document, dict)
            and isinstance(document.get("records"), list)
            and profile in {"auto", "aitim_archive_v1"}
        ):
            records = document["records"]
            mapping = {
                "name": "name",
                "unit": "unit",
                "price": "price",
                "product_url": "url",
                "price_date": "date",
                "seller": "organization",
                "seller_inn": "inn",
            }
            mapping.update(options.get("fields", {}))
            source = str(document.get("source") or "aitim_archive")
            profile = "aitim_archive_v1"
            requires = False
        elif isinstance(document, dict) and isinstance(document.get("items"), list):
            if document.get("schema_version") != 1 or not isinstance(document.get("source"), str):
                raise CatalogError("Canonical JSON требует schema_version=1 и source")
            records = document["items"]
            source = document["source"]
            profile = "catalog_items_v1"
            columns = list(
                dict.fromkeys(k for record in records if isinstance(record, dict) for k in record)
            )
            mapping, requires = self._mapping(columns, options)
        else:
            records = (
                document
                if isinstance(document, list)
                else document.get(options.get("records_key", "items"))
                if isinstance(document, dict)
                else None
            )
            if not isinstance(records, list):
                raise CatalogError("Не найден массив items/records; укажите профиль и records_key")
            columns = list(
                dict.fromkeys(k for record in records if isinstance(record, dict) for k in record)
            )
            mapping, requires = self._mapping(columns, options)
            requires = requires or not options.get("confirmed", False)
            source, profile = options.get("source", "user_upload"), "json_mapped"
        if not records:
            raise CatalogError("В файле нет записей")
        rows = []
        for number, record in enumerate(records):
            if not isinstance(record, dict):
                record = {"_raw_value": record}
            fields = self._map(record, mapping)
            issues = []
            if profile == "catalog_items_v1":
                issues = [
                    issue(
                        "schema_violation",
                        str(next(iter(error.path), "record")),
                        None,
                        error.message[:250],
                        "error",
                    )
                    for error in self.validator.iter_errors(record)
                ]
            rows.append(InputRow(number, number + 1, None, record, fields, issues))
        columns = list(
            dict.fromkeys(k for record in records if isinstance(record, dict) for k in record)
        )
        metadata = (
            {k: v for k, v in document.items() if k not in {"records", "items"}}
            if isinstance(document, dict)
            else {}
        )
        warnings = []
        if "total_unique_records" in metadata and metadata["total_unique_records"] != len(rows):
            warnings.append(
                {
                    "code": "reported_count_mismatch",
                    "reported": metadata["total_unique_records"],
                    "actual": len(rows),
                }
            )
        return ParsedCatalog(
            profile,
            source,
            rows,
            columns,
            mapping=mapping,
            requires_mapping=requires,
            metadata=metadata,
            warnings=warnings,
        )

    def _xml(self, raw: bytes, options: dict) -> ParsedCatalog:
        """Запрещает DTD/XXE/XInclude и валидирует каждую строку по локальному XSD."""
        try:
            root = SafeET.fromstring(
                raw, forbid_dtd=True, forbid_entities=True, forbid_external=True
            )
        except (DefusedXmlException, SafeET.ParseError, ValueError) as error:
            raise CatalogError("Небезопасный или некорректный XML") from error
        if root.tag != "catalog" or root.get("schema_version") != "1" or not root.get("source"):
            raise CatalogError("XML требует catalog schema_version=1 source=...")
        if any(
            "XInclude" in node.tag or "{http://www.w3.org/2001/XInclude}" in node.tag
            for node in root.iter()
        ):
            raise CatalogError("XInclude запрещён")
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        schema = etree.XMLSchema(etree.parse("docs/contracts/catalog_items_v1.xsd", parser))
        records, row_issues = [], []
        for element in root:
            if element.tag != "item":
                raise CatalogError("Ожидался XML item")
            record = {
                node.tag: [p.text or "" for p in node] if node.tag == "category_path" else node.text
                for node in element
            }
            sample = etree.Element("catalog", schema_version="1", source=root.get("source"))
            sample.append(etree.fromstring(SafeET.tostring(element), parser))
            errors = (
                []
                if schema.validate(sample)
                else [
                    issue(
                        "schema_violation", "record", None, "Строка не соответствует XSD", "error"
                    )
                ]
            )
            records.append(record)
            row_issues.append(errors)
        columns = list(dict.fromkeys(k for record in records for k in record))
        mapping, requires = self._mapping(columns, options)
        rows = [
            InputRow(i, i + 1, None, record, self._map(record, mapping), row_issues[i])
            for i, record in enumerate(records)
        ]
        if not rows:
            raise CatalogError("XML не содержит item")
        return ParsedCatalog(
            "catalog_items_v1",
            root.get("source"),
            rows,
            columns,
            mapping=mapping,
            requires_mapping=requires,
        )

    def _csv(self, raw: bytes, options: dict) -> ParsedCatalog:
        """Читает RFC-quoting, UTF-8 BOM и только явно выбранный CP1251."""
        encoding = options.get("encoding", "utf-8-sig")
        if encoding not in {"utf-8", "utf-8-sig", "cp1251"}:
            raise CatalogError("Выберите UTF-8 либо CP1251 явно")
        try:
            text = raw.decode(encoding)
        except UnicodeError as error:
            raise CatalogError("invalid_encoding: выберите кодировку CSV в настройках") from error
        if "\x00" in text:
            raise CatalogError("CSV содержит бинарные данные")
        delimiter = options.get("delimiter")
        if not delimiter:
            try:
                delimiter = csv.Sniffer().sniff(text[:8192], delimiters=";,\t").delimiter
            except csv.Error:
                delimiter = ";"
        if delimiter not in {";", ",", "\t"}:
            raise CatalogError("Неверный разделитель CSV")
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter, strict=True))
        except csv.Error as error:
            raise CatalogError("Некорректное quoting CSV") from error
        header_index = int(options.get("header_row", 1)) - 1
        if header_index < 0 or header_index >= len(rows):
            raise CatalogError("Строка заголовков отсутствует")
        columns = [str(v).strip() for v in rows[header_index]]
        if len(set(columns)) != len(columns) or not all(columns):
            raise CatalogError("Заголовки CSV должны быть непустыми и уникальными")
        mapping, requires = self._mapping(columns, options)
        parsed = []
        for i, values in enumerate(rows[header_index + 1 :], start=header_index + 2):
            if not any(values):
                continue
            record = dict(zip(columns, values, strict=False))
            if len(values) > len(columns):
                record["_extra_cells"] = values[len(columns) :]
            parsed.append(InputRow(len(parsed), i, None, record, self._map(record, mapping)))
        return ParsedCatalog(
            "csv_mapped",
            options.get("source", "user_upload"),
            parsed,
            columns,
            mapping=mapping,
            requires_mapping=requires,
            warnings=[{"encoding": encoding, "delimiter": delimiter}],
        )

    def _xlsx(self, path: Path, raw: bytes, options: dict) -> ParsedCatalog:
        """Читает выбранные листы read-only, не исполняет формулы, блокирует ZIP-bomb и макросы."""
        if not raw.startswith(b"PK"):
            raise CatalogError("Сигнатура XLSX не соответствует ZIP")
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                expanded = sum(entry.file_size for entry in entries)
                if (
                    expanded > self.settings.upload_zip_max_bytes
                    or expanded / max(len(raw), 1) > self.settings.upload_zip_max_ratio
                ):
                    raise CatalogError("XLSX превышает лимит распаковки ZIP")
                if any(
                    ".." in Path(entry.filename).parts
                    or entry.filename.startswith("/")
                    or "vbaProject" in entry.filename
                    for entry in entries
                ):
                    raise CatalogError("Небезопасный XLSX/макросы")
                if "xl/workbook.xml" not in archive.namelist():
                    raise CatalogError("ZIP не является XLSX")
        except zipfile.BadZipFile as error:
            raise CatalogError("Повреждён XLSX ZIP") from error
        workbook = None
        try:
            workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
            formulas = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        except (
            zipfile.BadZipFile,
            InvalidFileException,
            ValueError,
            KeyError,
            DefusedXmlException,
            SafeET.ParseError,
        ) as error:
            if workbook is not None:
                workbook.close()
            raise CatalogError("Некорректная структура XLSX") from error
        try:
            if len(workbook.sheetnames) > self.settings.upload_max_sheets:
                raise CatalogError("Слишком много листов")
            selected = options.get("sheets") or workbook.sheetnames[:1]
            if len(set(selected)) != len(selected):
                raise CatalogError("Лист XLSX выбран повторно")
            if any(sheet not in workbook.sheetnames for sheet in selected):
                raise CatalogError("Выбран неизвестный лист XLSX")
            rows, all_columns, last_mapping, requires = [], [], {}, False
            cell_count = 0
            for sheet_name in selected:
                sheet = workbook[sheet_name]
                form = formulas[sheet_name]
                if sheet.max_row > self.settings.upload_max_rows + 1000 or sheet.max_column > 512:
                    raise CatalogError("Размер таблицы XLSX превышает лимит")
                header = int(options.get("header_row", 1))
                if header < 1 or header > 1000:
                    raise CatalogError("Неверная строка заголовков XLSX")
                columns = [
                    str(cell.value or "").strip()
                    for cell in next(sheet.iter_rows(min_row=header, max_row=header))
                ]
                while columns and not columns[-1]:
                    columns.pop()
                if not columns or not all(columns) or len(set(columns)) != len(columns):
                    raise CatalogError("XLSX требует уникальные непустые заголовки")
                mapping, missing = self._mapping(columns, options)
                requires = requires or missing
                all_columns.extend(c for c in columns if c not in all_columns)
                last_mapping = mapping
                for cells, formula_cells in zip(
                    sheet.iter_rows(min_row=header + 1),
                    form.iter_rows(min_row=header + 1),
                    strict=True,
                ):
                    cell_count += len(cells)
                    if cell_count > self.settings.upload_max_cells:
                        raise CatalogError("Превышен лимит ячеек")
                    if not any(cell.value is not None for cell in cells) and not any(
                        cell.data_type == "f" for cell in formula_cells
                    ):
                        continue
                    record = {
                        column: json_value(cells[i].value) for i, column in enumerate(columns)
                    }
                    issues = []
                    for i, cell in enumerate(formula_cells[: len(columns)]):
                        if cell.data_type == "f":
                            record.setdefault("_formulas", {})[cell.coordinate] = cell.value
                            if cells[i].value is None:
                                issues.append(
                                    issue(
                                        "formula_without_cached_value",
                                        columns[i],
                                        cell.value,
                                        "Формула без сохранённого результата; не исполнялась",
                                    )
                                )
                    rows.append(
                        InputRow(
                            len(rows),
                            cells[0].row,
                            sheet_name,
                            record,
                            self._map(record, mapping),
                            issues,
                        )
                    )
            return ParsedCatalog(
                "xlsx_mapped",
                options.get("source", "user_upload"),
                rows,
                all_columns,
                workbook.sheetnames,
                last_mapping,
                requires or (len(workbook.sheetnames) > 1 and "sheets" not in options),
            )
        finally:
            workbook.close()
            formulas.close()


def sample_catalog(format_name: str) -> tuple[bytes, str]:
    """Формирует синтетические шаблоны скачивания, не зависящие от локального examples/."""
    record = {
        "source_key": "sample-001",
        "name": "Кабель тестовый",
        "unit": "м",
        "quantity": "2",
        "price": "12.50",
        "currency": "RUB",
        "price_date": "2024-11-18",
    }
    if format_name == "json":
        return json.dumps(
            {"schema_version": 1, "source": "synthetic_example", "items": [record]},
            ensure_ascii=False,
            indent=2,
        ).encode(), "application/json"
    if format_name == "xml":
        root = Element("catalog", schema_version="1", source="synthetic_example")
        item = SubElement(root, "item")
        for key, value in record.items():
            SubElement(item, key).text = value
        return SafeET.tostring(root, encoding="utf-8", xml_declaration=True), "application/xml"
    if format_name == "csv":
        text = io.StringIO(newline="")
        writer = csv.DictWriter(text, fieldnames=list(record), delimiter=";")
        writer.writeheader()
        writer.writerow(record)
        return text.getvalue().encode("utf-8-sig"), "text/csv"
    if format_name == "xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        workbook.active.title = "Каталог"
        workbook.active.append(list(record))
        workbook.active.append(list(record.values()))
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()
        return (
            output.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    raise CatalogError("Неизвестный формат шаблона")
