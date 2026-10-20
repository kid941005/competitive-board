"""外部链接清洗。

「来源链接」「官方链接」这些字段的值来自导入表格、页面表单和 API，最终会被渲染成
`<a href="...">`。写入时不校验协议，一个 `javascript:alert(1)` 就会在有人点「链接」
时执行 —— 目前只有管理员能写，不构成越权，但没有理由把这类值存进库里。

规则：
- 空 → None
- `http://` / `https://` → 原样
- 站内相对路径（以单个 `/` 开头）→ 原样，供 `/datasheet/<id>/file` 这类内部链接使用
- 裸域名（`www.murata.com/x.pdf`）→ 补 `https://`
- 其它（`javascript:`、`data:`、`//host`、含反斜杠的伪相对路径）→ None
"""

from __future__ import annotations

ALLOWED_SCHEMES = ("http://", "https://")


def safe_url(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if "\\" in text:  # 浏览器会把 /\host 当 //host
        return None
    low = text.lower()
    if low.startswith("//"):  # 协议相对地址，无法判断来源
        return None
    if low.startswith("/"):  # 站内相对路径
        return text
    if low.startswith(ALLOWED_SCHEMES):
        return text
    if ":" in low.split("/", 1)[0]:  # 带 scheme 但不是 http(s)
        return None
    return "https://" + text
