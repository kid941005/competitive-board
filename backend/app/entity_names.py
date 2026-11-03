"""公司名归一规则：只保留用于「是否同一家公司」的比较键。

页面、API、规格书导入与表格导入共用这一份规则，避免「村田制作所（Murata）」
在一个入口认成村田、另一个入口又建出第二份档案。
"""

from __future__ import annotations

import re

_PAREN = re.compile(r"[（(][^（()）]*[)）]")
_NOISE = re.compile(r"[\s\-_.·/\\()（）&]+")


def key(name: str) -> str:
    """忽略空白、标点、大小写与括号补充说明。"""
    text = _PAREN.sub("", name or "")
    return _NOISE.sub("", text).casefold()
