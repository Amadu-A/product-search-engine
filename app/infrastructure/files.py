# app/infrastructure/files.py
"""Локальные JSON/HTML: исходные байты, отдельная нормализация и защищённый bundle."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, Field

from app.domain.taxonomy import (
    FileProof,
    Node,
    Snapshot,
    SourceMetadata,
    TaxonomyError,
    canonical_hash,
    category_id,
    snapshot_hash,
    validate_tree,
)


class InputNode(BaseModel):
    """Независимый от поставщика формат вложенного исходного дерева."""

    model_config = ConfigDict(extra="forbid")
    external_id: str | None = None
    name: str
    source_url: str | None = None
    kind: str = "category"
    children: list["InputNode"] = Field(default_factory=list)


class InputTree(BaseModel):
    """Фактические метаданные и декларация полноты, которая не блокирует импорт."""

    model_config = ConfigDict(extra="forbid")
    source: SourceMetadata
    complete: bool
    nodes: list[InputNode]


class CollectorReport(BaseModel):
    """Проверяет типы счётчиков сборщика, сохраняя все дополнительные исходные поля."""

    model_config = ConfigDict(extra="allow")
    remaining_pages: int = Field(default=0, ge=0, strict=True)
    errors: list[dict] = Field(default_factory=list)
    redirects: list[dict] = Field(default_factory=list)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """Не допускает молчаливую подмену повторяющегося ключа внутри JSON-объекта."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise TaxonomyError("Повтор ключа внутри JSON-объекта")
        result[key] = value
    return result


def read_json(data: bytes) -> object:
    """Разбирает JSON без неоднозначных дублирующихся полей."""
    return json.loads(data, object_pairs_hook=_unique_object)


def read_limited(path: Path, max_bytes: int) -> bytes:
    """Ограничивает объём входа до JSON/HTML parsing, включая обычные файлы."""
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise TaxonomyError("Исходный файл превышает лимит размера")
    return data


def write_json(path: Path, value: object) -> None:
    """Атомарно заменяет JSON-файл внутри каталога рабочих артефактов."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def html_nodes(data: bytes) -> list[InputNode]:
    """Читает явный ul[data-taxonomy], не угадывая структуру по всем ссылкам страницы.

    Каждый li содержит одну прямую a либо span[data-name] и необязательный ul детей.
    Неизвестная вёрстка, товарные узлы и неоднозначность приводят к отказу.
    """
    soup = BeautifulSoup(data, "html.parser")
    roots = soup.select("ul[data-taxonomy]")
    if len(roots) != 1:
        raise TaxonomyError("HTML должен содержать один ul[data-taxonomy]; вёрстка не распознана")
    return _read_list(roots[0], 1)


def _read_list(container: Tag, depth: int) -> list[InputNode]:
    """Восстанавливает только прямые связи вложенных списков с ограничением глубины."""
    if depth > 128:
        raise TaxonomyError("Слишком глубокий HTML")
    items = container.find_all("li", recursive=False)
    if not items:
        raise TaxonomyError("Пустой список категорий в HTML")
    result = []
    for item in items:
        labels = item.select(":scope > a, :scope > span[data-name]")
        nested = item.find_all("ul", recursive=False)
        if len(labels) != 1 or len(nested) > 1 or item.get("data-kind", "category") != "category":
            raise TaxonomyError("Неоднозначный или товарный узел HTML")
        label = labels[0]
        result.append(
            InputNode(
                external_id=item.get("data-external-id"),
                name=label.get_text(" ", strip=True),
                source_url=label.get("href"),
                children=_read_list(nested[0], depth + 1) if nested else [],
            )
        )
    return result


def normalize_file(
    input_path: Path,
    output_dir: Path,
    max_bytes: int,
    metadata_path: Path | None = None,
    max_nodes: int = 20000,
    max_depth: int = 32,
    collector_report_path: Path | None = None,
) -> Snapshot:
    """Сохраняет исходники без изменений и отдельно нормализует дерево, включая неполное.

    Из названий удаляются только крайние/повторные пробельные символы. Цены не
    вырезаются: подозрительные названия получают признаки качества и предупреждение.
    Отчёт сборщика сохраняется целиком, а его ошибки не становятся ошибками дерева.
    """
    outputs = {
        output_dir / name
        for name in (
            "categories.json",
            "categories.sha256",
            "normalization_report.json",
        )
    }
    inputs = [p for p in (input_path, metadata_path, collector_report_path) if p is not None]
    if any(p.resolve() == target.resolve() for p in inputs for target in outputs):
        raise TaxonomyError("Выходной файл совпадает с исходником; используйте другой каталог")
    raw = read_limited(input_path, max_bytes)
    raw_hash = hashlib.sha256(raw).hexdigest()
    proofs = []
    if input_path.suffix.lower() in {".html", ".htm"}:
        if metadata_path is None:
            raise TaxonomyError("HTML требует --metadata с условиями использования и complete")
        meta_raw = read_limited(metadata_path, max_bytes)
        metadata = read_json(meta_raw)
        tree = InputTree.model_validate({**metadata, "nodes": html_nodes(raw)})
        method = "semantic-html"
        metadata_hash = hashlib.sha256(meta_raw).hexdigest()
        meta_name = f"raw/{metadata_hash}.json"
        (output_dir / "raw").mkdir(parents=True, exist_ok=True)
        (output_dir / meta_name).write_bytes(meta_raw)
        proofs.append(FileProof(file=meta_name, sha256=metadata_hash))
    else:
        tree = InputTree.model_validate(read_json(raw))
        method = "structured-json"
    collector = None
    collector_raw = None
    if collector_report_path is not None:
        collector_raw = read_limited(collector_report_path, max_bytes)
        collector = read_json(collector_raw)
        CollectorReport.model_validate(collector)
        collector_hash = hashlib.sha256(collector_raw).hexdigest()
        proofs.append(FileProof(file=f"raw/{collector_hash}.collector.json", sha256=collector_hash))
    extracted_at = datetime.now(UTC)
    flat = []
    normalizations = []
    stack = [(n, None, (), order) for order, n in reversed(list(enumerate(tree.nodes)))]
    while stack:
        current, parent, path, order = stack.pop()
        if current.kind != "category":
            raise TaxonomyError("Товарный узел среди категорий")
        node_id = category_id(tree.source.key, current.external_id, current.source_url)
        name = " ".join(current.name.split())
        full_path = (*path, name)
        if name != current.name:
            normalizations.append(
                {
                    "external_id": current.external_id,
                    "field": "name",
                    "before": current.name,
                    "after": name,
                    "operation": "normalize_whitespace",
                }
            )
        if len(flat) >= max_nodes or len(full_path) > max_depth:
            raise TaxonomyError("Превышен лимит узлов или глубины")
        flat.append(
            Node(
                id=node_id,
                external_id=current.external_id,
                name=name,
                parent_id=parent,
                level=len(full_path),
                path=full_path,
                source_url=current.source_url,
                sort_order=order,
                extracted_at=extracted_at,
                source=tree.source.key,
            )
        )
        stack.extend(
            (child, node_id, full_path, index)
            for index, child in reversed(list(enumerate(current.children)))
        )
    raw_name = f"raw/{raw_hash}{'.html' if method == 'semantic-html' else '.json'}"
    proofs.append(FileProof(file=raw_name, sha256=raw_hash))
    snapshot = Snapshot(
        source=tree.source,
        extracted_at=extracted_at,
        method=method,
        complete=tree.complete,
        raw_sha256=canonical_hash([p.model_dump() for p in proofs]),
        files=tuple(proofs),
        nodes=tuple(flat),
        diagnostics={
            "input_sha256": raw_hash,
            "collector_report": collector,
            "normalizations": normalizations,
        },
    )
    metrics = validate_tree(snapshot, max_nodes, max_depth)
    (output_dir / "raw").mkdir(parents=True, exist_ok=True)
    (output_dir / raw_name).write_bytes(raw)
    if collector_raw is not None:
        (output_dir / f"raw/{collector_hash}.collector.json").write_bytes(collector_raw)
    write_json(output_dir / "categories.json", snapshot.model_dump(mode="json"))
    (output_dir / "categories.sha256").write_text(snapshot_hash(snapshot) + "\n", encoding="ascii")
    write_json(
        output_dir / "normalization_report.json",
        {
            "input_sha256": raw_hash,
            "snapshot_sha256": snapshot_hash(snapshot),
            "complete": snapshot.complete,
            "normalization_count": len(normalizations),
            "normalizations": normalizations,
            "quality": metrics["quality"],
            "warnings": metrics["warnings"],
            "collection": metrics["collection"],
        },
    )
    return snapshot


def load_snapshot(path: Path, max_bytes: int) -> Snapshot:
    """Проверяет JSON, checksum и каждый исходный файл, запрещая выход за пределы bundle."""
    snapshot = Snapshot.model_validate_json(read_limited(path, max_bytes))
    expected = path.with_suffix(".sha256").read_text(encoding="ascii").strip()
    if expected != snapshot_hash(snapshot):
        raise TaxonomyError("Контрольная сумма categories.json не совпадает")
    root = path.parent.resolve()
    for proof in snapshot.files:
        raw_path = (root / proof.file).resolve()
        if not raw_path.is_relative_to(root) or raw_path == root:
            raise TaxonomyError("Исходный файл вне каталога снимка")
        if hashlib.sha256(read_limited(raw_path, max_bytes)).hexdigest() != proof.sha256:
            raise TaxonomyError("Контрольная сумма исходного файла не совпадает")
    return snapshot
