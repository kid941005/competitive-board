"""数值解析：把用户/表格里写的数字转成 float。

单独成一个模块，是因为「1,5 是 1.5 还是 15」这个问题在三个入口都出现
（手工编辑、Excel/CSV 导入、API），而三处各写一份必然走样。
"""

from __future__ import annotations


def parse_number(raw: object) -> float | None:
    """把单元格/表单里的文本转成 float；无法识别返回 None。

    逗号有两种含义，按位判断而不是一律当千分位：
      "1,234.56" → 1234.56（逗号是千分位）
      "1,5"      → 1.5     （只有逗号、且后面 ≤2 位，视为小数点）
      "1,234"    → 1234    （只有逗号、后面 3 位，视为千分位）

    早期实现直接 replace(",", "")，于是欧洲写法 "1,5" 被解析成 15 —— 差 10 倍，
    而且不报错，是那种「数据错了半年才发现」的问题。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)

    text = str(raw).strip().replace(" ", "").replace("\u3000", "")
    if not text:
        return None

    if "," in text:
        if "." in text:
            # 逗号只能是千分位
            text = text.replace(",", "")
        else:
            head, _, tail = text.rpartition(",")
            if head and tail.isdigit() and len(tail) <= 2:
                text = f"{head}.{tail}"
            else:
                text = text.replace(",", "")

    try:
        return float(text)
    except ValueError:
        return None
