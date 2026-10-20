"""导出：把对比矩阵写成 Excel。

为什么不用 CSV 顶替：Excel 才承载得住「谁最优、谁没公开、谁没复核」这三层信息 ——
CSV 里这些只能退化成文字，回到 Excel 又得手工上色。所以导出保留格式：

- 加粗浅绿底 = 该指标最优值（按指标自身的 higher/lower 方向判定，单一值不给最优）
- 「—」灰字 = 厂商未公开
- 橙色字 = 未复核（人工需逐项确认）
- 底部完整度行 = 该型号已公开项占比
"""

from __future__ import annotations

import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .compare import better_label

BEST_FILL = PatternFill("solid", fgColor="E7F6EC")
HEAD_FILL = PatternFill("solid", fgColor="EEF2FF")
NOTE_FONT = Font(color="777777", size=9)
MISSING_FONT = Font(color="AAAAAA")
UNVERIFIED_FONT = Font(color="C2410C")
THIN = Side(style="thin", color="E2E4E9")
BORDER = Border(bottom=THIN)


def _width(text: str) -> int:
    """中文按 2 个字符宽度估算列宽。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(text or ""))


def matrix_xlsx(data: dict, *, generated_at: datetime | None = None) -> bytes:
    """data 为 main._matrix_payload 的返回值。返回 xlsx 字节。"""
    wb = Workbook()
    products = data["products"]
    ent_by_id = data["ent_by_id"]
    rows = data["rows"]
    comp = data.get("completeness") or {}

    # ── 参数矩阵 ──────────────────────────────────────────────────
    ws = wb.active
    ws.title = "参数对比"

    header = ["指标组", "指标", "单位", "优劣"]
    for pr in products:
        ent = ent_by_id.get(pr.entity_id)
        header.append(f"{ent.name if ent else ''}\n{pr.model}")
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 34

    for row in rows:
        metric = row.metric
        line = [metric.category, metric.name, metric.unit, better_label(metric.better)]
        for pr in products:
            cell = row.cells.get(pr.id)
            if cell is None or cell.is_missing:
                line.append("—")
            else:
                line.append(cell.display)
        ws.append(line)

        r = ws.max_row
        for c in range(1, 5):
            ws.cell(r, c).border = BORDER
        for idx, pr in enumerate(products):
            cell = row.cells.get(pr.id)
            xcell = ws.cell(r, 5 + idx)
            xcell.border = BORDER
            xcell.alignment = Alignment(horizontal="center" if len(str(xcell.value or "")) < 10 else "left")
            if cell is None or cell.is_missing:
                xcell.font = MISSING_FONT
            elif pr.id in row.best:
                xcell.font = Font(bold=True)
                xcell.fill = BEST_FILL
            elif not cell.verified:
                xcell.font = UNVERIFIED_FONT

    # 完整度
    ws.append(["完整度", "已公开项占比", "%", ""] + [f"{comp.get(pr.id, 0) * 100:.0f}%" for pr in products])
    r = ws.max_row
    for c in range(1, 5 + len(products)):
        ws.cell(r, c).font = Font(bold=True)
        ws.cell(r, c).border = Border(top=Side(style="medium", color="C7CBD3"))

    note_row = ws.max_row + 2
    ws.cell(note_row, 1, "说明：加粗浅绿=该指标最优值（单一有效值不判定最优）；「—」=厂商未公开；橙字=未复核，使用前需核对原厂资料。")
    ws.cell(note_row, 1).font = NOTE_FONT

    # 列宽 / 冻结 / 筛选
    ws.column_dimensions["A"].width = max(_width(x) for x in ["指标组"] + [r.metric.category for r in rows]) + 4
    ws.column_dimensions["B"].width = max(_width(x) for x in ["指标"] + [r.metric.name for r in rows]) + 4
    ws.column_dimensions["C"].width = 9
    ws.column_dimensions["D"].width = 10
    for idx, pr in enumerate(products):
        ent = ent_by_id.get(pr.entity_id)
        label = f"{ent.name if ent else ''} {pr.model}"
        ws.column_dimensions[get_column_letter(5 + idx)].width = max(14, _width(label) + 4)
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = f"A1:{get_column_letter(4 + len(products))}{1 + len(rows)}"

    # ── 型号清单 ──────────────────────────────────────────────────
    ws2 = wb.create_sheet("型号清单")
    ws2.append(["公司", "型号", "产品大类", "类别", "完整度", "已公开项"])
    for cell in ws2[1]:
        cell.font = Font(bold=True)
        cell.fill = HEAD_FILL
    kind_label = data.get("kind_label") or {}
    cat_label = data.get("cat_all") or {}
    for pr in products:
        ent = ent_by_id.get(pr.entity_id)
        pct = comp.get(pr.id, 0)
        shown = sum(
            1 for row in rows
            if row.cells.get(pr.id) is not None and not row.cells[pr.id].is_missing
        )
        ws2.append([
            ent.name if ent else "",
            pr.model,
            cat_label.get(pr.category or "", pr.category or ""),
            kind_label.get(ent.kind if ent else "", ""),
            pct,
            shown,
        ])
        ws2.cell(ws2.max_row, 5).number_format = "0%"
    for col, w in zip("ABCDEF", (22, 16, 14, 10, 10, 12), strict=True):
        ws2.column_dimensions[col].width = w
    ws2.freeze_panes = "A2"

    # ── 说明 ──────────────────────────────────────────────────────
    ws3 = wb.create_sheet("说明")
    ts = (generated_at or datetime.now()).strftime("%Y-%m-%d %H:%M")
    lines = [
        ("竞品参数对比导出", ""),
        ("生成时间", ts),
        ("型号数", str(len(products))),
        ("指标行数", str(len(rows))),
        ("", ""),
        ("最优值", "按各指标自身的优劣方向判定；只有一个有效值时不标最优（那是「唯一」不是「最优」）。"),
        ("缺失", "「—」表示厂商未公开，计入完整度但不参与最优判定。缺失本身就是情报。"),
        ("未复核", "橙色数值来自自动识别或二手资料，使用前需按原厂数据手册核对。"),
        ("完整度", "该型号已公开项 / 当前视图的指标总项。"),
    ]
    for row in lines:
        ws3.append(row)
    ws3["A1"].font = Font(bold=True, size=13)
    for cell in ws3["A"]:
        if cell.row > 1:
            cell.font = Font(bold=True)
    ws3.column_dimensions["A"].width = 16
    ws3.column_dimensions["B"].width = 86
    for row in ws3.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
