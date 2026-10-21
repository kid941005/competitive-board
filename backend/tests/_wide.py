"""测试辅助：按宽表格式拼 CSV。

导入模板已经改成宽表（一行一个型号、指标是列），测试里不该再手写 24 个表头 ——
表头一改，几十处字符串全要跟着改。这里按 `importer.WIDE_HEADERS` 生成，
测试只表达「这一行想写哪几格」。

用法：
    from _wide import wide_csv
    text = wide_csv({"企业名称": "华芯微电子", "产品型号": "SX-1", "流量L/min": "0.7"})
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.importer import WIDE_HEADERS


def wide_csv(*rows: dict) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=WIDE_HEADERS, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def wide_headers() -> list[str]:
    return list(WIDE_HEADERS)
