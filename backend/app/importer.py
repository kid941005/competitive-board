"""CSV 导入 —— P0 阶段手工维护参数的主通道（Excel 另存为 CSV 即可）。

列（表头必须存在，顺序不限）：
    型号,友商,指标,数值,单位,条件,来源链接,置信度

规则：
- 指标按 key 或中文名匹配指标字典；**匹配不到直接报错跳过，不自动新建**。
  这是刻意的：指标字典必须先想清楚，否则会退化成每个友商一套字段。
- 产品按型号匹配；不存在则连同友商一起创建。
- 数值为空表示「该型号未公开此项」，写入时跳过（不覆盖已有值）。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlmodel import Session, select

from .models import Entity, Product, ProductSpec, SpecMetric, SpecSource

REQUIRED_COLS = ("型号", "指标", "数值")


@dataclass
class ImportReport:
    created_products: int = 0
    created_entities: int = 0
    written_specs: int = 0
    skipped_missing: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "created_products": self.created_products,
            "created_entities": self.created_entities,
            "written_specs": self.written_specs,
            "skipped_missing": self.skipped_missing,
            "errors": self.errors,
        }


def _to_float(raw: str) -> float | None:
    s = (raw or "").strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def import_csv(session: Session, text: str) -> ImportReport:
    report = ImportReport()
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLS if c not in headers]
    if missing:
        report.errors.append(f"缺少必需列：{', '.join(missing)}（实际表头：{headers}）")
        return report

    metrics = session.exec(select(SpecMetric)).all()
    metric_index: dict[str, SpecMetric] = {}
    for m in metrics:
        metric_index[m.key.strip().lower()] = m
        metric_index[m.name.strip().lower()] = m

    entity_cache: dict[str, Entity] = {
        e.name.strip().lower(): e for e in session.exec(select(Entity)).all()
    }
    product_cache: dict[str, Product] = {
        p.model.strip().lower(): p for p in session.exec(select(Product)).all()
    }

    for lineno, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        model = row.get("型号", "")
        metric_raw = row.get("指标", "")
        value_raw = row.get("数值", "")

        if not model or not metric_raw:
            report.errors.append(f"第 {lineno} 行：型号或指标为空，已跳过")
            continue

        metric = metric_index.get(metric_raw.lower())
        if metric is None:
            report.errors.append(
                f"第 {lineno} 行：指标「{metric_raw}」不在指标字典中，已跳过（请先在字典里定义）"
            )
            continue

        if not value_raw:
            report.skipped_missing += 1
            continue

        # 友商 / 产品
        ent_name = row.get("友商", "") or "未分组"
        entity = entity_cache.get(ent_name.lower())
        if entity is None:
            entity = Entity(kind="competitor", name=ent_name)
            session.add(entity)
            session.commit()
            session.refresh(entity)
            entity_cache[ent_name.lower()] = entity
            report.created_entities += 1

        product = product_cache.get(model.lower())
        if product is None:
            product = Product(entity_id=entity.id, model=model, category=row.get("大类") or None)
            session.add(product)
            session.commit()
            session.refresh(product)
            product_cache[model.lower()] = product
            report.created_products += 1

        # 来源
        url = row.get("来源链接", "")
        source = SpecSource(
            kind="manual",
            url=url or None,
            title=f"{model} 手工录入",
            captured_at=datetime.now(timezone.utc),
        )
        session.add(source)
        session.commit()
        session.refresh(source)

        num = _to_float(value_raw)
        is_number = metric.value_type == "number"
        if is_number and num is None:
            report.errors.append(
                f"第 {lineno} 行：指标「{metric.name}」要求数值，但收到「{value_raw}」，已跳过"
            )
            continue

        existing = session.exec(
            select(ProductSpec).where(
                ProductSpec.product_id == product.id,
                ProductSpec.metric_id == metric.id,
            )
        ).first()
        if existing:
            existing.value_num = num if is_number else None
            existing.value_text = None if is_number else value_raw
            existing.condition = row.get("条件") or None
            existing.source_id = source.id
            existing.confidence = _to_float(row.get("置信度", "")) or 1.0
            existing.updated_at = datetime.now(timezone.utc)
            session.add(existing)
        else:
            session.add(
                ProductSpec(
                    product_id=product.id,
                    metric_id=metric.id,
                    value_num=num if is_number else None,
                    value_text=None if is_number else value_raw,
                    condition=row.get("条件") or None,
                    source_id=source.id,
                    confidence=_to_float(row.get("置信度", "")) or 1.0,
                    verified=False,
                )
            )
        report.written_specs += 1

    session.commit()
    return report


CSV_TEMPLATE = (
    "型号,友商,指标,数值,单位,条件,来源链接,置信度\n"
    "SX-2400,华芯微电子,插入损耗,1.80,dB,@2.4GHz,https://example.com/sx-2400.pdf,0.95\n"
    "SX-2400,华芯微电子,封装形式,SMD 1411,,,https://example.com/sx-2400.pdf,0.95\n"
)
