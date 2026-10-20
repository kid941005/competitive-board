"""关键词检索的公共约定 —— 只做「怎么把用户输入变成匹配」，不碰 HTTP。

三件事必须统一，否则各处的搜索行为会各不相同：

1. **输入归一**：去首尾空白、限长（防止一个超长 q 拼出巨大的 LIKE）、空串视为没搜。
2. **LIKE 转义**：`%` 与 `_` 是通配符。原来 `q=%` 会匹配全部、`q=_` 匹配任意单字符，
   用户以为搜到了东西，其实什么都没过滤。这里转义后一律按字面量搜。
3. **多词 AND**：`村田 MZB` 应当同时命中两个词，而不是要求连在一起。

字段层面：型号、产品名、公司名（含英文名）、指标 key/名称/别名都参与匹配，
匹配到哪个字段会在接口里回 `matched`，方便调用方解释「为什么这条会出现」。
"""

from __future__ import annotations

MAX_QUERY_LEN = 64
# LIKE 的转义符：SQL 片段里要写成 ESCAPE '\'，对应 SQLAlchemy 的 escape="\\"
ESCAPE = "\\"


def normalize(raw: str | None) -> str | None:
    """空串/纯空白 → None；超长截断；首尾空白去掉。"""
    text = (raw or "").strip()
    if not text:
        return None
    return text[:MAX_QUERY_LEN]


def escape_like(text: str) -> str:
    """把 % _ \\ 变成字面量。反斜杠要先处理，否则会把自己加的转义符再转一遍。"""
    return text.replace(ESCAPE, ESCAPE * 2).replace("%", ESCAPE + "%").replace("_", ESCAPE + "_")


def pattern(keyword: str) -> str:
    """给 `ilike(pattern, escape=ESCAPE)` 用的子串模式。"""
    return f"%{escape_like(keyword)}%"


def tokens(keyword: str | None) -> list[str]:
    """按空白切词并小写；空则返回空列表。"""
    return [t for t in (keyword or "").lower().split() if t]


def hits(*fields: str | None, keyword: str | None) -> bool:
    """多词 AND：每个词都必须出现在这些字段拼起来的文本里。没有关键词时永远为真。"""
    words = tokens(keyword)
    if not words:
        return True
    haystack = " ".join((f or "") for f in fields).lower()
    return all(word in haystack for word in words)


def matched_fields(keyword: str | None, **fields: str | None) -> list[str]:
    """哪些字段命中了关键词（命中任意一个词即算）。

    用途是解释「这条为什么出现」，所以刻意比 hits() 宽松：
    `村田 MZB` 这种跨字段的组合里，公司名只命中前半个词、型号只命中后半个，
    两个字段都该出现在解释里，而不是因为「没有哪个字段同时含两个词」就返回空。
    """
    words = tokens(keyword)
    if not words:
        return []
    out: list[str] = []
    for name, value in fields.items():
        low = (value or "").lower()
        if low and any(word in low for word in words):
            out.append(name)
    return out
