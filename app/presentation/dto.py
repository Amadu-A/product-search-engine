# app/presentation/dto.py
"""Публичные response DTO Swagger для прогресса, позиций, preview и SQL-дерева."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class JobView(BaseModel):
    """Разделяет счётчики исходных строк и объектов каталога."""

    model_config = ConfigDict(extra="allow")
    id: UUID
    status: Literal[
        "uploaded",
        "validating",
        "preview_ready",
        "queued",
        "running",
        "completed",
        "completed_with_issues",
        "failed",
        "canceled",
        "cancel_requested",
    ]
    mapping_version: int
    total_count: int
    processed_count: int
    assigned_count: int
    auto_assigned_count: int = 0
    manual_assigned_count: int = 0
    assigned_without_product_count: int = 0
    products_created_count: int = 0
    categorized_products_count: int = 0
    not_product_count: int = 0
    archived_not_product_count: int = 0
    technical_error_count: int = 0
    needs_review_count: int
    taxonomy_gap_count: int
    error_count: int
    cancel_requested: bool
    created_at: datetime
    mode: Literal["mock", "live"] | None = None


class FileView(BaseModel):
    """Файл с SHA и preview; физический storage path наружу не передаётся."""

    model_config = ConfigDict(extra="allow")
    id: UUID
    original_filename: str
    content_sha256: str
    byte_size: int
    format: str
    profile: str | None
    mapping: dict | None
    preview: dict


class PreviewView(BaseModel):
    """Возможность start зависит от подтверждённого mapping всех файлов."""

    job: JobView
    files: list[FileView]
    can_start: bool
    mode: Literal["mock", "live"]


class ItemView(BaseModel):
    """Позиция сохраняет собственный status отдельно от Product/Classification."""

    model_config = ConfigDict(extra="allow")
    id: UUID
    job_id: UUID
    file_id: UUID
    item_position: int
    processing_version: int
    status: Literal[
        "pending",
        "normalizing",
        "looking_up_product",
        "understanding",
        "matching",
        "classifying",
        "assigned",
        "needs_review",
        "taxonomy_gap",
        "not_product",
        "invalid_record",
        "error",
    ]
    issue_codes: list[str]
    product_id: UUID | None
    category_id: UUID | None
    normalized_record_id: UUID | None
    raw_record_id: UUID | None
    name: str | None = None
    category_name: str | None = None
    assignment_origin: Literal["manual", "auto"] | None = None


class ItemsPage(BaseModel):
    """Серверная pagination с фактическим числом совпавших позиций."""

    items: list[ItemView]
    total: int
    offset: int
    limit: int


class TreeNodeView(BaseModel):
    """Иерархия берётся только из PostgreSQL, с исходными устойчивыми UUID."""

    model_config = ConfigDict(extra="allow")
    id: UUID
    parent_id: UUID | None
    name: str
    slug: str
    is_active: bool
    is_assignable: bool
    review_status: Literal["provisional", "approved", "deprecated"]
    path_ids: list[UUID]
    path_names: list[str]
    aliases: list[str] = Field(default_factory=list)
    product_count: int
    subtree_product_count: int
    depth: int
    ancestors_active: bool


class ProductView(BaseModel):
    """Поля строки каталога из Product и его справочников."""

    id: UUID
    canonical_name: str
    category_id: UUID | None
    category_name: str | None
    manufacturer_id: UUID | None
    manufacturer_name: str | None
    manufacturer_status: str | None
    brand_id: UUID | None
    brand_name: str | None
    brand_status: str | None
    mpn: str | None
    normalized_mpn: str | None
    model: str | None
    product_type: str | None
    unit_code: str | None
    status: str
    attributes: dict
    created_at: datetime
    updated_at: datetime


class ProductsPage(BaseModel):
    """Результат поиска и стабильной SQL-пагинации."""

    items: list[ProductView]
    total: int
    offset: int
    limit: int


class ProductFeature(BaseModel):
    """Характеристика изделия, сохранённая при понимании номенклатуры."""

    name: str
    value: str
    unit: str | None = None


class SourceLink(BaseModel):
    """Исходная ссылка, сохранённая при импорте."""

    url: str
    source_name: str


class ProductDetailView(ProductView):
    """Карточка с путём категории, характеристиками и исходными ссылками."""

    category_path: list[str]
    features: list[ProductFeature]
    source_links: list[SourceLink]


class ManufacturerView(BaseModel):
    """Отображаемое имя производителя и статус его сведений."""

    id: UUID
    name: str
    status: str


class PriceObservationView(BaseModel):
    """Историческое наблюдение, его исходные поля и сведения о верификации."""

    id: UUID
    raw_price: str | None
    amount: Decimal | None
    currency: str | None
    sale_unit: str | None
    source_reported_date: date | None
    observed_at: datetime
    quality_status: str
    valid_from: datetime | None
    valid_to: datetime | None
    verified_amount: Decimal | None
    verified_currency: str | None
    verified_unit: str | None


class OfferView(BaseModel):
    """Сведения о продавце и источнике с ограниченной историей цен."""

    id: UUID
    seller_name: str | None
    source_name: str
    origin_url: str | None
    sale_unit: str | None
    pack_quantity: Decimal | None
    price_history: list[PriceObservationView]
    history_total: int


class OffersPage(BaseModel):
    """Сохранённые предложения и история наблюдений по одному товару."""

    items: list[OfferView]
    total: int
    offset: int
    limit: int
    price_policy: Literal["historical"] = "historical"
