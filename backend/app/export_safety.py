"""Spreadsheet exports preserve user text without activating formulas."""
from __future__ import annotations

import io
import math
import re

from openpyxl.cell import Cell

_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def append_row(sheet, values) -> None:
    """Explicit text cells prevent formula inference and invalid XML crashes."""
    row = []
    for value in values:
        if isinstance(value, str):
            cell = Cell(sheet, value=_INVALID_XML.sub("�", value))
            cell.data_type = "s"
            row.append(cell)
        else:
            row.append(value)
    sheet.append(row)


def xlsx_bytes(workbook) -> bytes:
    # openpyxl infers '=...' as a formula on assignment. The board exports data,
    # so force those cells to explicit strings, preserving exact text on re-import.
    for sheet in workbook:
        for row in sheet:
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def csv_text(value: str) -> str:
    candidate = value.lstrip()
    # 括号写清楚：两个条件是「或」，不能靠 and/or 的优先级读出来
    dangerous = bool(candidate) and candidate[0] in "=+-@"
    if dangerous or value.startswith(("\t", "\r", "\n")):
        try:
            if math.isfinite(float(value)):
                return value
        except ValueError:
            pass
        return "'" + value
    return value
