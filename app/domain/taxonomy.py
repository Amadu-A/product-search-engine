# app/domain/taxonomy.py
"""Контракт снимка и проверка леса категорий до любых изменений PostgreSQL."""

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field


class TaxonomyError(ValueError):
    """Останавливает импорт при нарушении структуры, прав или целостности данных."""


class Contract(BaseModel):
    """Запрещает неизвестные поля и случайные изменения проверенного контракта."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def category_url(value: str) -> str:
    """Проверяет абсолютную ссылку происхождения без credentials и параметров."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise TaxonomyError("Недопустимый HTTPS URL категории")
    return value


def stable_key(external_id: str | None, url: str | None) -> str:
    """Использует исходный ID, а при его отсутствии стабильный канонический URL."""
    if url is not None:
        category_url(url)
    if external_id is not None:
        if (
            not external_id.strip()
            or external_id != external_id.strip()
            or len(external_id.encode("utf-8")) > 512
        ):
            raise TaxonomyError("Пустой или ненормализованный внешний ID")
        return external_id
    if url is None:
        raise TaxonomyError("Нужен исходный ID или канонический URL")
    if len(url.encode("utf-8")) > 512:
        raise TaxonomyError("URL слишком длинный для стабильного индексируемого ключа")
    return "url:" + url


def category_id(source: str, external_id: str | None, url: str | None) -> UUID:
    """Получает воспроизводимый UUIDv5 из источника и устойчивого внешнего ключа."""
    return uuid5(NAMESPACE_URL, source + ":" + stable_key(external_id, url))


class Node(Contract):
    """Плоский узел снимка с явным родителем и полным путём от корня."""

    id: UUID
    external_id: str | None
    name: str
    parent_id: UUID | None
    level: int = Field(ge=1)
    path: tuple[str, ...]
    source_url: str | None
    sort_order: int = Field(ge=0)
    extracted_at: datetime
    source: str
    kind: Literal["category"] = "category"

    def imported_values(self) -> dict:
        """Возвращает управляемые импортом поля для сравнения с последним baseline."""
        key = stable_key(self.external_id, self.source_url)
        return {
            "id": str(self.id),
            "parent_id": str(self.parent_id) if self.parent_id else None,
            "name": self.name,
            "slug": self.source + "-" + hashlib.sha256(key.encode()).hexdigest()[:24],
            "sort_order": self.sort_order,
            "source": self.source,
            "external_id": key,
            "source_url": self.source_url,
            "is_active": True,
        }


class FileProof(Contract):
    """Контрольная сумма исходных байтов, сохранённых рядом со снимком."""

    file: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SourceMetadata(Contract):
    """Условия использования конкретного источника, независимые от нашей иерархии."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1)
    license: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    attribution: str
    license_notice: str
    usage_status: Literal["unverified", "licensed", "approved", "synthetic"]

    def require_reuse(self, permission: "Permission | None") -> None:
        """Блокирует неподтверждённое использование и требует основание для approved."""
        if self.key == "custom":
            raise TaxonomyError("Источник custom зарезервирован для пользовательских категорий")
        if not all(value.strip() for value in (self.title, self.license, self.reference)):
            raise TaxonomyError("Не заполнены сведения о происхождении")
        if self.usage_status in {"unverified", "approved"} or self.key == "petrovich":
            if permission is None or permission.source != self.key:
                raise TaxonomyError("BLOCKED: необходимо документированное разрешение источника")
            permission.require("reuse")
        elif self.license in {"NOASSERTION", "UNKNOWN"} or not self.license_notice.strip():
            raise TaxonomyError("Не сохранены условия и лицензионное уведомление источника")


class Sample(Contract):
    """Контрольная категория, независимо сверенная с сайтом либо разрешённым экспортом."""

    id: UUID
    name: str
    path: tuple[str, ...]
    source_url: str | None


class Review(Contract):
    """Подтверждает полноту и контрольную выборку именно данного JSON-снимка."""

    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewed_by: str = Field(min_length=3)
    reviewed_at: datetime
    reference: str = Field(min_length=10)
    complete: Literal[True]
    samples: tuple[Sample, ...] = Field(min_length=1)


class Permission(Contract):
    """Документированное основание доступа; наличие файла не отменяет robots/403."""

    source: str
    approved_by: str = Field(min_length=3)
    approved_at: datetime
    reference: str = Field(min_length=10)
    allow_fetch: bool
    allow_reuse: bool

    def require(self, operation: Literal["fetch", "reuse"]) -> None:
        """Требует явно разрешённую операцию, валидную дату и непустое основание."""
        check_date(self.approved_at)
        if len(self.reference.strip()) < 10 or len(self.approved_by.strip()) < 3:
            raise TaxonomyError("Не указано основание разрешения")
        if not (self.allow_fetch if operation == "fetch" else self.allow_reuse):
            raise TaxonomyError(f"BLOCKED: отсутствует разрешение на {operation}")


class Snapshot(Contract):
    """Версия плоского дерева и исходные файлы; сырой SHA считается по manifest."""

    schema_version: Literal[1] = 1
    source: SourceMetadata
    extracted_at: datetime
    method: Literal["semantic-html", "structured-json", "petrovich-html"]
    complete: bool
    raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    files: tuple[FileProof, ...] = Field(min_length=1)
    nodes: tuple[Node, ...]


def check_date(value: datetime) -> None:
    """Требует timezone-aware дату, не допускает будущее в подтверждающих документах."""
    if value.tzinfo is None or value > datetime.now(UTC):
        raise TaxonomyError("Дата должна содержать часовой пояс и не быть в будущем")


def canonical_hash(value: object) -> str:
    """Вычисляет SHA-256 канонического JSON независимо от пробелов в исходном файле."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def snapshot_hash(snapshot: Snapshot) -> str:
    """Связывает проверку полноты и импорт с точной версией снимка."""
    return canonical_hash(snapshot.model_dump(mode="json"))


def validate_tree(snapshot: Snapshot, max_nodes: int = 20000, max_depth: int = 32) -> dict:
    """Проверяет связность, циклы, ключи, пути и manifest; возвращает реальные метрики.

    Неполный снимок и неоднозначное родительство блокируются до открытия транзакции.
    Полнота относительно сайта дополнительно подтверждается отдельным Review.
    """
    check_date(snapshot.extracted_at)
    nodes = snapshot.nodes
    if not snapshot.complete or not nodes or len(nodes) > max_nodes:
        raise TaxonomyError("Пустое, неполное или превышающее лимит дерево")
    indexed = {node.id: node for node in nodes}
    keys = [stable_key(n.external_id, n.source_url) for n in nodes]
    urls = [n.source_url for n in nodes if n.source_url is not None]
    if len(indexed) != len(nodes) or len(set(keys)) != len(nodes) or len(set(urls)) != len(urls):
        raise TaxonomyError("Дубли UUID, внешних ID или URL")
    sibling_order: set[tuple[UUID | None, int]] = set()
    for node in nodes:
        check_date(node.extracted_at)
        if node.extracted_at != snapshot.extracted_at:
            raise TaxonomyError("Дата узла отличается от даты снимка")
        if not node.name.strip() or node.name != node.name.strip():
            raise TaxonomyError("Пустое или ненормализованное название")
        if node.source != snapshot.source.key or node.source == "custom":
            raise TaxonomyError("Источник узла не совпадает с метаданными")
        if node.id != category_id(node.source, node.external_id, node.source_url):
            raise TaxonomyError("UUID не соответствует стабильному ключу")
        order_key = (node.parent_id, node.sort_order)
        if order_key in sibling_order:
            raise TaxonomyError("Повтор порядка отображения у одного родителя")
        sibling_order.add(order_key)
        chain: list[Node] = []
        seen: set[UUID] = set()
        current = node
        while True:
            if current.id in seen:
                raise TaxonomyError("Цикл в дереве")
            seen.add(current.id)
            chain.append(current)
            if len(chain) > max_depth:
                raise TaxonomyError("Превышена максимальная глубина")
            if current.parent_id is None:
                break
            if current.parent_id not in indexed:
                raise TaxonomyError("Потерянный родитель")
            current = indexed[current.parent_id]
        if node.level != len(chain) or node.path != tuple(n.name for n in reversed(chain)):
            raise TaxonomyError("Неверный уровень или полный путь")
    validate_manifest(snapshot)
    parents = {n.parent_id for n in nodes}
    return {
        "total": len(nodes),
        "roots": sum(n.parent_id is None for n in nodes),
        "leaves": sum(n.id not in parents for n in nodes),
        "max_depth": max(n.level for n in nodes),
        "levels": dict(sorted(Counter(n.level for n in nodes).items())),
        "duplicates": 0,
        "cycles": 0,
        "orphans": 0,
        "errors": 0,
    }


def validate_manifest(snapshot: Snapshot) -> None:
    """Проверяет SHA manifest; байты исходных файлов дополнительно проверяет адаптер."""
    if len({p.file for p in snapshot.files}) != len(snapshot.files):
        raise TaxonomyError("Повтор исходного файла")
    raw_hash = canonical_hash([p.model_dump(mode="json") for p in snapshot.files])
    if snapshot.raw_sha256 != raw_hash:
        raise TaxonomyError("Контрольная сумма manifest не совпадает")


def validate_review(snapshot: Snapshot, review: Review) -> None:
    """Не позволяет применить чужую проверку или контрольную выборку с расхождениями."""
    check_date(review.reviewed_at)
    if len(review.reference.strip()) < 10 or len(review.reviewed_by.strip()) < 3:
        raise TaxonomyError("Не заполнено основание проверки дерева")
    if review.reviewed_at < snapshot.extracted_at:
        raise TaxonomyError("Проверка не может предшествовать извлечению")
    if review.snapshot_sha256 != snapshot_hash(snapshot):
        raise TaxonomyError("Review относится к другому снимку")
    indexed = {n.id: n for n in snapshot.nodes}
    if len({s.id for s in review.samples}) != len(review.samples):
        raise TaxonomyError("Повтор категории в контрольной выборке")
    sampled_roots: set[UUID] = set()
    for sample in review.samples:
        node = indexed.get(sample.id)
        if node is None or (node.name, node.path, node.source_url) != (
            sample.name,
            sample.path,
            sample.source_url,
        ):
            raise TaxonomyError("Контрольная выборка не совпадает с источником")
        while node.parent_id:
            node = indexed[node.parent_id]
        sampled_roots.add(node.id)
    if sampled_roots != {n.id for n in snapshot.nodes if n.parent_id is None}:
        raise TaxonomyError("Контрольная выборка должна охватывать каждую корневую ветвь")
