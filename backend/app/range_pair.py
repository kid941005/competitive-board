"""范围指标（工作温度）的上下限配对。

工作温度天生是个区间（-40~+85 ℃）。只存一端，另一端就只活在「测试条件」那行
文字里 —— 对比页上既看不到、也比不了：HTPF 系列 -40 与村田 0 的低温能力差 40 ℃，
但两行都只显示上限，看不出来。

配对关系放这一处，三条写入通道共用：
  · 表格导入（`importer`）：一格拆两格
  · 规格书识别后确认写入（`routes_datasheet`）：从识别出的原文区间补出下限
  · 迁移 v5（`schema`）：从存量「测试条件」回填下限
"""

from __future__ import annotations

import re

# 上限指标 → 下限指标。目前只有温度，未来若有别的「区间指标」也往这里加。
RANGE_PAIRS: dict[str, str] = {"op_temp_max": "op_temp_min"}

# 区间写法：0~45、-40~+85、5–50、0至60、-55~-20
_RANGE = re.compile(
    r"([-+]?\d+(?:\.\d+)?)\s*[~～\-–—至]\s*([-+]?\d+(?:\.\d+)?)"
)


def parse_range(text: str | None) -> tuple[float, float] | None:
    """从「原文 -40~+85」这类文字里取出 (下限, 上限)；取不到返回 None。

    两个数大小反了就交换 —— 库里存的是语义上的下限/上限，不是原文顺序。
    """
    m = _RANGE.search(text or "")
    if m is None:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    return (min(a, b), max(a, b))


def lower_metric_key(metric_key: str) -> str | None:
    """该指标是否属于范围配对；是则返回对应的下限指标 key。"""
    return RANGE_PAIRS.get(metric_key)
