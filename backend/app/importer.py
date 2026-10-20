"""表格导入（Excel / CSV）—— 扩充数据库的批量通道。

列（表头必须存在「型号」「指标」「数值」三列，其余可选，顺序不限）：

    型号, 友商, 大类, 指标, 数值, 单位, 条件, 来源链接, 置信度

规则：
- 指标按 key 或中文名匹配指标字典；**匹配不到直接报错跳过，不自动新建**。
  这是刻意的：指标字典必须先想清楚，否则会退化成每个友商一套字段。
- 产品按型号匹配；不存在则连同友商一起创建。
- 友商名做归一匹配（去空白标点、忽略大小写），避免「村田」和「村田制作所」各建一份。
- 大类只接受配置里的代码或中文名；填了非法值就报错，**不猜**。
- 数值为空表示「该型号未公开此项」，写入时跳过（不覆盖已有值）。

Excel 与 CSV 走同一套校验：读取器只负责把文件变成「表头 + 行字典」，之后完全一致。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlmodel import Session, select

from . import config
from .models import Entity, Product, ProductSpec, SpecMetric, SpecSource

# 规范列名 → 可接受的写法（中英文都收，避免用户改表头就导入失败）
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "型号": ("型号", "产品型号", "model", "part number", "pn"),
    "友商": ("友商", "公司", "厂商", "企业", "entity", "company", "brand"),
    "大类": ("大类", "产品大类", "类别", "category"),
    "指标": ("指标", "参数", "指标名", "metric", "parameter"),
    "数值": ("数值", "值", "measure", "value"),
    "单位": ("单位", "unit", "units"),
    "条件": ("条件", "测试条件", "condition", "test condition"),
    "来源链接": ("来源链接", "来源", "链接", "url", "source", "source url"),
    "置信度": ("置信度", "可信度", "confidence"),
}

REQUIRED_COLS = ("型号", "指标", "数值")

_ALIAS_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in COLUMN_ALIASES.items():
    for _a in _aliases:
        _ALIAS_LOOKUP[_a.strip().lower()] = _canonical


@dataclass
class ImportReport:
    rows_read: int = 0
    created_products: int = 0
    created_entities: int = 0
    written_specs: int = 0
    skipped_missing: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rows_read": self.rows_read,
            "created_products": self.created_products,
            "created_entities": self.created_entities,
            "written_specs": self.written_specs,
            "skipped_missing": self.skipped_missing,
            "errors": self.errors,
        }


def _norm_key(s: str) -> str:
    return re.sub(r"[\s\-_.·/\\()（）]+", "", (s or "").strip()).lower()


def normalize_header(raw: str) -> str | None:
    """把表头单元格映射成规范列名；认不出来返回 None。"""
    key = (raw or "").strip().lower().replace(" ", "").replace("_", "")
    if not key:
        return None
    for alias, canonical in _ALIAS_LOOKUP.items():
        if key == alias.replace(" ", "").replace("_", ""):
            return canonical
    return None


def _to_float(raw: str) -> float | None:
    s = (raw or "").strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def resolve_category(raw: str) -> str | None:
    """把「压电气泵 / pump_air / air pump」这类写法归一成代码；不认识返回 None。"""
    s = (raw or "").strip()
    if not s:
        return None
    if s in config.CATEGORY_LABEL:
        return s
    low = s.lower().replace(" ", "").replace("_", "")
    for code, label in config.CATEGORY_LABEL.items():
        if low == label.lower() or low == code.replace("_", ""):
            return code
    # 常见别名：气泵 / 风扇 / 液泵
    short = {
        "气泵": "pump_air", "空气泵": "pump_air", "airpump": "pump_air",
        "风扇": "fan", "散热风扇": "fan",
        "液泵": "pump_liquid", "水泵": "pump_liquid", "liquidpump": "pump_liquid",
    }
    return short.get(low)


def rows_from_csv(text: str) -> tuple[list[dict], list[str]]:
    """解析 CSV 文本，返回 (行字典列表, 已识别的规范表头)。"""
    reader = csv.reader(io.StringIO(text))
    try:
        raw_header = next(reader)
    except StopIteration:
        return [], []
    mapping = {i: normalize_header(h) for i, h in enumerate(raw_header)}
    headers = [v for v in mapping.values() if v]
    rows: list[dict] = []
    for raw in reader:
        if not any((c or "").strip() for c in raw):
            continue
        row: dict[str, str] = {}
        for i, cell in enumerate(raw):
            canonical = mapping.get(i)
            if canonical:
                row[canonical] = (cell or "").strip()
        rows.append(row)
    return rows, headers


def rows_from_xlsx(data: bytes) -> tuple[list[dict], list[str]]:
    """解析 xlsx 第一个工作表，返回 (行字典列表, 已识别的规范表头)。

    表头不必在第 1 行：向下找第一行含「型号」的行，兼容带标题/说明的手工表。
    """
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        mapping: dict[int, str] = {}
        headers: list[str] = []
        rows: list[dict] = []
        header_found = False
        for values in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v).strip() for v in values]
            if not any(cells):
                continue
            if not header_found:
                cand = {i: normalize_header(c) for i, c in enumerate(cells)}
                recognized = [v for v in cand.values() if v]
                # 至少认出 2 列、且含「型号」或「指标」，才算表头行。
                # 这样既能跳过标题/说明行，又不会把「缺列」的情况吞成「没读到数据」。
                if len(recognized) >= 2 and ("型号" in recognized or "指标" in recognized):
                    mapping = cand
                    headers = recognized
                    header_found = True
                continue
            row: dict[str, str] = {}
            for i, cell in enumerate(cells):
                canonical = mapping.get(i)
                if canonical and cell:
                    row[canonical] = cell
            if row:
                rows.append(row)
        return rows, headers
    finally:
        wb.close()


def _source_cache(session: Session, batch_note: str) -> dict[str, SpecSource]:
    """同一批里相同链接只建一条来源，避免几百行生成几百条一模一样的出处。"""
    cache: dict[str, SpecSource] = {}

    def get(url: str) -> SpecSource:
        if url in cache:
            return cache[url]
        src = SpecSource(
            kind="manual",
            url=url or None,
            title="表格导入",
            captured_at=datetime.now(timezone.utc),
            note=batch_note or None,
        )
        session.add(src)
        session.commit()
        session.refresh(src)
        cache[url] = src
        return src

    return get


def import_rows(
    session: Session, rows: list[dict], headers: list[str], *, batch_note: str = ""
) -> ImportReport:
    report = ImportReport(rows_read=len(rows))
    missing = [c for c in REQUIRED_COLS if c not in headers]
    if missing:
        report.errors.append(
            f"缺少必需列：{', '.join(missing)}（实际表头：{headers}）"
        )
        return report

    metrics = session.exec(select(SpecMetric)).all()
    metric_index: dict[str, SpecMetric] = {}
    for m in metrics:
        metric_index[m.key.strip().lower()] = m
        metric_index[m.name.strip().lower()] = m

    entity_cache: dict[str, Entity] = {
        _norm_key(e.name): e for e in session.exec(select(Entity)).all()
    }
    product_cache: dict[str, Product] = {
        p.model.strip().lower(): p for p in session.exec(select(Product)).all()
    }
    source_for = _source_cache(session, batch_note)

    for lineno, row in enumerate(rows, start=2):
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

        # 大类：填了就必须认得出，认不出不猜
        category_raw = row.get("大类", "")
        category = None
        if category_raw:
            category = resolve_category(category_raw)
            if category is None:
                options = "、".join(f"{label}（{code}）" for code, label in config.CATEGORY_LABEL.items())
                report.errors.append(
                    f"第 {lineno} 行：大类「{category_raw}」不可识别，已跳过。可选：{options}"
                )
                continue

        if not value_raw:
            report.skipped_missing += 1
            continue

        # 友商 / 产品
        ent_name = row.get("友商", "") or "未分组"
        entity = entity_cache.get(_norm_key(ent_name))
        if entity is None:
            entity = Entity(kind="competitor", name=ent_name)
            session.add(entity)
            session.commit()
            session.refresh(entity)
            entity_cache[_norm_key(ent_name)] = entity
            report.created_entities += 1

        product = product_cache.get(model.lower())
        if product is None:
            product = Product(entity_id=entity.id, model=model, category=category)
            session.add(product)
            session.commit()
            session.refresh(product)
            product_cache[model.lower()] = product
            report.created_products += 1
        elif category:
            # 已有型号不能改换大类：那会让历史参数落到错误的指标体系里
            if product.category and product.category != category:
                report.errors.append(
                    f"第 {lineno} 行：型号 {model} 已属「{config.category_label(product.category)}」，"
                    f"与表中「{config.category_label(category)}」冲突，已跳过"
                )
                continue
            if not product.category:
                product.category = category
                session.add(product)
                session.commit()
                session.refresh(product)

        num = _to_float(value_raw)
        is_number = metric.value_type == "number"
        if is_number and num is None:
            report.errors.append(
                f"第 {lineno} 行：指标「{metric.name}」要求数值，但收到「{value_raw}」，已跳过"
            )
            continue

        source = source_for(row.get("来源链接", ""))
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


def import_csv(session: Session, text: str, *, batch_note: str = "CSV 导入") -> ImportReport:
    rows, headers = rows_from_csv(text)
    return import_rows(session, rows, headers, batch_note=batch_note)


def import_xlsx(session: Session, data: bytes, *, batch_note: str = "Excel 导入") -> ImportReport:
    rows, headers = rows_from_xlsx(data)
    if not rows and not headers:
        rep = ImportReport()
        rep.errors.append("没有读到任何数据：请确认第一个工作表里存在「型号」「指标」表头")
        return rep
    return import_rows(session, rows, headers, batch_note=batch_note)


# 模板表头与示例行（Excel / CSV 共用）
TEMPLATE_HEADER = ["型号", "友商", "大类", "指标", "数值", "单位", "条件", "来源链接", "置信度"]
TEMPLATE_ROWS = [
    ["MZB1001T02", "村田制作所", "压电气泵", "空气流量", 0.7, "L/min", "@15Vpp",
     "https://www.murata.com/zh-cn/products/mechatronics/fluid", 1.0],
    ["MZB1001T02", "村田制作所", "压电气泵", "静压", 1.42, "kPa", "@15Vpp",
     "https://www.murata.com/zh-cn/products/mechatronics/fluid", 1.0],
    ["PH-200", "深圳新锐压电", "压电风扇", "风量", 1.8, "L/min", "",
     "", 1.0],
]

CSV_TEMPLATE = (
    ",".join(TEMPLATE_HEADER)
    + "\n"
    + "\n".join(",".join(str(c) for c in row) for row in TEMPLATE_ROWS)
    + "\n"
)


def xlsx_template() -> bytes:
    """生成带表头样式与说明页的 xlsx 模板。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "参数导入"

    head_fill = PatternFill("solid", fgColor="EEF2FF")
    ws.append(TEMPLATE_HEADER)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center")
    for row in TEMPLATE_ROWS:
        ws.append(row)

    widths = [16, 18, 12, 14, 10, 10, 12, 44, 10]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    tip = wb.create_sheet("填写说明")
    lines = [
        ("列", "说明"),
        ("型号", "必填。已存在则更新该型号参数；不存在则新建型号。"),
        ("友商", "公司名称。已存在则复用（忽略空格、标点与大小写）；不存在则新建公司。"),
        ("大类", "可选，但强烈建议填。只能填：" + "、".join(config.CATEGORY_LABEL.values())),
        ("指标", "必填。填指标中文名或 key，必须在指标字典中存在，否则该行跳过。"),
        ("数值", "必填。数值型指标填数字；文本型指标填原文（如 24~27）。留空表示未公开，跳过。"),
        ("单位", "仅作说明，实际单位以指标字典为准；填错不会改变换算。"),
        ("条件", "如 @15Vpp、水·100Hz，会显示在溯源浮层。"),
        ("来源链接", "越具体越好，便于逐项复核。"),
        ("置信度", "0~1，缺省 1.0。"),
        ("", ""),
        ("注意", "导入的参数默认标记「未复核」，需在「产品管理」里逐项确认。"),
        ("注意", "型号不能改换大类：已有型号若与表中大类冲突，该行会报错跳过。"),
    ]
    for row in lines:
        tip.append(row)
    for cell in tip[1]:
        cell.font = Font(bold=True)
    tip.column_dimensions["A"].width = 16
    tip.column_dimensions["B"].width = 88

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
