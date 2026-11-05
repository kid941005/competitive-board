"""V1 API request contracts, independent of routing and persistence."""
from __future__ import annotations

from pydantic import BaseModel, Field

from . import config, tokens


class EntityIn(BaseModel):
    name: str
    kind: str = "competitor"
    name_en: str | None = None
    region: str | None = None
    tier: str = "C"
    force: bool = False
    dry_run: bool = False


class ProductIn(BaseModel):
    entity_id: int | None = None
    entity_name: str | None = None
    model: str
    category: str
    name: str | None = None
    status: str = "active"
    force: bool = False
    dry_run: bool = False


class ProductPatch(BaseModel):
    model: str | None = None
    name: str | None = None
    category: str | None = None
    status: str | None = None
    datasheet_url: str | None = None
    note: str | None = None
    dry_run: bool = False


class SpecItem(BaseModel):
    product_id: int
    metric_key: str
    action: str = "upsert"  # upsert / delete
    value_num: float | None = Field(default=None, allow_inf_nan=False)
    value_text: str | None = None
    # 传了 unit 就按它换算到指标字典的单位（mL/min → L/min、Vp → Vpp）；
    # 不传视为已经是字典单位。跨维度或认不出的单位会让该项被拒，不会按原值落库。
    unit: str | None = None
    condition: str | None = None
    verified: bool = False
    confidence: float | None = Field(default=None, allow_inf_nan=False)


class SpecBatch(BaseModel):
    items: list[SpecItem] = Field(max_length=config.IMPORT_MAX_ROWS)
    dry_run: bool = False


class ImportIn(BaseModel):
    rows: list[dict] = Field(max_length=config.IMPORT_MAX_ROWS)
    note: str | None = None
    dry_run: bool = False


class EntityPatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    name_en: str | None = None
    region: str | None = None
    tier: str | None = None
    dry_run: bool = False


class AdoptionIn(BaseModel):
    product_id: int
    customer_id: int | None = None
    customer_name: str | None = None
    status: str = "sample"
    year: int | None = None
    note: str | None = None
    dry_run: bool = False


class AdoptionPatch(BaseModel):
    status: str | None = None
    year: int | None = None
    note: str | None = None


class TokenIn(BaseModel):
    name: str
    # 不要写可变默认值：Pydantic 会复制，但读代码的人不该靠「框架替我兜住了」来判断
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    expires_days: int | None = Field(default=None, ge=1, le=tokens.MAX_EXPIRY_DAYS)
