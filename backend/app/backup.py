"""全量备份导出：「导出全部」按钮背后的实现。

与 `exporters.matrix_xlsx` 的区别：那个是**报表**（给当前选中的几个型号，带最优高亮、
缺失灰显、未复核橙字），这个是**备份**（全部数据、无样式，目的是可读 + 可再导入）。

三个工作表：

1. `参数导入` —— 与导入模板同形（一行一个型号、指标是列）。**导出后可以直接再导入**，
   所以它同时是备份包和迁移包。数值按列头声明的单位导出（静压按 Pa），
   再导入时由 `importer` 换算回字典单位，往返不会漂。
2. `参数明细` —— 一行一个「型号 × 指标」，带单位、测试条件、来源链接、置信度、复核状态、
   更新时间。宽表看不出的东西全在这里；它是给人核对的，不用于再导入。
3. `客户采用` —— 客户 × 型号 × 进度 × 年份 × 备注。

刻意不做样式与合并单元格：备份文件要能被别的工具读，花哨格式反而是负担。
"""

from __future__ import annotations

import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlmodel import select

from . import range_pair, units
from .adoptions import status_label
from .importer import WIDE_COLUMNS, metric_for_column
from .models import Adoption, Entity, Product, ProductSpec, SpecMetric, SpecSource

HEAD_FILL = PatternFill("solid", fgColor="EEF2FF")


def _rows(session, model) -> list:
    return list(session.exec(select(model)).all())


def _data(session) -> dict:
    """一次把要用的表全读出来，避免在双层循环里逐行查库（型号 × 指标会放大成 N+1）。"""
    entities = {e.id: e for e in _rows(session, Entity)}
    products = sorted(
        _rows(session, Product),
        key=lambda p: (entities[p.entity_id].name if p.entity_id in entities else "",
                       (p.category or ""), p.model or ""),
    )
    metrics = {m.id: m for m in _rows(session, SpecMetric)}
    specs: dict[int, dict[int, ProductSpec]] = {}
    for sp in _rows(session, ProductSpec):
        specs.setdefault(sp.product_id, {})[sp.metric_id] = sp
    sources = {s.id: s for s in _rows(session, SpecSource)}
    adoptions = _rows(session, Adoption)
    return {
        "entities": entities, "products": products, "metrics": metrics,
        "specs": specs, "sources": sources, "adoptions": adoptions,
    }


def _range_text(lo_spec, hi_spec) -> str:
    """范围列（工作温度）导出成区间，与导入同形 —— 否则导出一份就丢了下限。

    优先用「测试条件」里记下的原文（`原文 -40~+85` → 取 `-40~+85`）：这样导出再导入
    写出来的 condition 与原来一字不差，往返是零写入。只有在原文与当前上下限对不上
    （有人在页面上改过值）时，才按数值重新拼 —— 否则会导出一个陈旧的区间。
    """
    lo = lo_spec.value_num if lo_spec is not None else None
    hi = hi_spec.value_num if hi_spec is not None else None
    if lo is None and hi is None:
        return ""
    if lo is None:
        return f"{hi:g}"
    if hi is None:
        return f"{lo:g}"

    cond = ((hi_spec.condition if hi_spec is not None else None) or "").strip()
    if cond.startswith("原文 "):
        literal = cond[len("原文 "):].strip()
        if range_pair.parse_range(literal) == (lo, hi):
            return literal
    return f"{lo:g}~{hi:g}"


def _head(ws, headers: list[str]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def export_all_xlsx(session, *, generated_at: datetime | None = None) -> bytes:
    data = _data(session)
    entities, products = data["entities"], data["products"]
    metrics, specs = data["metrics"], data["specs"]
    metrics_by_key = {m.key: m for m in metrics.values()}
    sources, adoptions = data["sources"], data["adoptions"]

    wb = Workbook()

    # ── 1. 参数导入（宽表，与模板同形，可再导入）───────────────────────────
    ws = wb.active
    ws.title = "参数导入"
    _head(ws, [c.header for c in WIDE_COLUMNS])

    for p in products:
        ent = entities.get(p.entity_id)
        row_specs = specs.get(p.id, {})
        values: list[object] = [ent.name if ent else "", p.model, p.category or ""]
        for col in WIDE_COLUMNS[3:]:
            if col.kind == "meta":
                values.append(_meta_value(col.field, p, row_specs, metrics, sources, entities,
                                          adoptions))
                continue
            # 与导入同一套映射：液泵的流量在宽表里也走「流量L/min」这一列
            metric = metric_for_column(col, metrics_by_key, p.category)
            spec = row_specs.get(metric.id) if metric else None
            if col.range_min_key:
                lo_metric = metrics_by_key.get(col.range_min_key)
                lo_spec = row_specs.get(lo_metric.id) if lo_metric else None
                values.append(_range_text(lo_spec, spec))
                continue
            values.append(_cell_value(spec, metric, col.unit))
        ws.append(values)

    for i, col in enumerate(WIDE_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = col.width
    ws.freeze_panes = "C2"

    # ── 2. 参数明细（一行一个 型号 × 指标）───────────────────────────────
    ws2 = wb.create_sheet("参数明细")
    _head(ws2, ["企业名称", "产品型号", "产品类别", "指标 key", "指标名", "数值", "文本",
                "单位", "测试条件", "来源链接", "置信度", "复核", "更新时间"])
    for p in products:
        ent = entities.get(p.entity_id)
        for mid, spec in sorted(specs.get(p.id, {}).items()):
            metric = metrics.get(mid)
            if metric is None:
                continue
            src = sources.get(spec.source_id) if spec.source_id else None
            ws2.append([
                ent.name if ent else "", p.model, p.category or "",
                metric.key, metric.name,
                spec.value_num if spec.value_num is not None else "",
                spec.value_text or "", metric.unit, spec.condition or "",
                (src.url or "") if src else "", spec.confidence,
                "是" if spec.verified else "否",
                spec.updated_at.strftime("%Y-%m-%d %H:%M") if spec.updated_at else "",
            ])
    for i, width in enumerate([22, 18, 12, 16, 12, 10, 22, 10, 20, 44, 8, 6, 16], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = width
    ws2.freeze_panes = "A2"

    # ── 3. 客户采用 ────────────────────────────────────────────────────
    ws3 = wb.create_sheet("客户采用")
    _head(ws3, ["客户", "产品型号", "所属公司", "状态", "年份", "备注", "更新时间"])
    prod_by_id = {p.id: p for p in products}
    for a in adoptions:
        cust = entities.get(a.customer_id)
        prod = prod_by_id.get(a.product_id)
        owner = entities.get(prod.entity_id) if prod else None
        ws3.append([
            cust.name if cust else f"#{a.customer_id}",
            prod.model if prod else f"#{a.product_id}",
            owner.name if owner else "",
            status_label(a.status), a.year or "", a.note or "",
            a.updated_at.strftime("%Y-%m-%d %H:%M") if a.updated_at else "",
        ])
    for i, width in enumerate([24, 20, 20, 10, 8, 40, 16], start=1):
        ws3.column_dimensions[get_column_letter(i)].width = width
    ws3.freeze_panes = "A2"

    # ── 4. 说明 ────────────────────────────────────────────────────────
    ws4 = wb.create_sheet("说明")
    ts = (generated_at or datetime.now()).strftime("%Y-%m-%d %H:%M")
    confidential = sum(1 for p in products for s in specs.get(p.id, {}).values() if s.verified)
    lines = [
        ("全量备份", ""),
        ("生成时间", ts),
        ("公司 / 型号 / 参数", f"{len(entities)} / {len(products)} / "
                              f"{sum(len(v) for v in specs.values())}"),
        ("客户采用", str(len(adoptions))),
        ("", ""),
        ("参数导入", "与导入模板同形，可直接再导入（覆盖同名型号的同名参数，值没变的不动）。"),
        ("参数明细", "一行一个「型号 × 指标」，含单位 / 测试条件 / 来源 / 置信度 / 复核状态，"
                    "用于核对，不用于再导入。"),
        ("单位", "参数导入表按列头单位导出（静压按 Pa、液体流量按 µL/min），"
                "再导入时会换算回系统口径（kPa、µL/min），往返不会漂。"),
        ("复核", f"当前有 {confidential} 个参数已人工复核；导入会把被覆盖的参数重置为未复核。"),
        ("不含", "登录账号、API 令牌、变更日志、系统参数 —— 这些不属于业务数据，"
                "账号与令牌请另行保管。"),
    ]
    for row in lines:
        ws4.append(list(row))
    ws4["A1"].font = Font(bold=True, size=13)
    for cell in ws4["A"]:
        if cell.row > 1:
            cell.font = Font(bold=True)
    ws4.column_dimensions["A"].width = 18
    ws4.column_dimensions["B"].width = 88
    for row in ws4.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _cell_value(spec: ProductSpec | None, metric: SpecMetric | None, column_unit: str) -> object:
    """一个指标格导成什么。数值按列头单位导出，保证往返一致。"""
    if spec is None or metric is None:
        return ""
    if metric.value_type == "number":
        if spec.value_num is None:
            return ""
        out = spec.value_num
        if column_unit:
            conv = units.convert(spec.value_num, metric.unit, column_unit)
            if conv.ok:
                out = conv.value
        return round(out, 6)
    return spec.value_text or ""


def _meta_value(field_name, product, row_specs, metrics, sources, entities, adoptions) -> object:
    """宽表里的非指标列。"""
    if field_name == "note":
        return product.note or ""
    if field_name == "confidence":
        confs = [s.confidence for s in row_specs.values() if s.confidence is not None]
        return round(max(confs), 2) if confs else ""
    if field_name == "source":
        for spec in row_specs.values():
            src = sources.get(spec.source_id) if spec.source_id else None
            if src and src.url:
                return src.url
        return ""
    if field_name == "customers":
        names = []
        for a in adoptions:
            if a.product_id != product.id:
                continue
            cust = entities.get(a.customer_id)
            label = cust.name if cust else f"#{a.customer_id}"
            names.append(f"{label}（{a.note}）" if a.note else label)
        return "；".join(names)
    return ""
