"""时间显示：库里统一存 UTC，界面统一按部署时区渲染。

为什么单独成模块：数据库里所有时间都是 UTC（`models.utcnow()`），但模板里到处
`strftime` 直出 —— 于是 19:27 做的改动在变更日志里显示成 11:27。存 UTC 是对的，
错在「存了 UTC 却当本地时间打印」。

约定：
- 写入侧永远存 UTC，不改成 naive，也不改表结构；
- 读取侧只经这里转成本地时区再显示，展示格式由调用方给；
- 取不到 `TZ` 对应的时区数据时回落固定 +08:00（中国无夏令时，等价）。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone


def _resolve_tz():
    name = (os.getenv("TZ") or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # pragma: no cover - 缺 tzdata 的 Windows 环境
        return timezone(timedelta(hours=8))


APP_TZ = _resolve_tz()


def as_aware(value: datetime | None) -> datetime | None:
    """SQLite 读回来的时间是 naive 的，按既定约定视为 UTC。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def to_local(value: datetime | None) -> datetime | None:
    dt = as_aware(value)
    return dt.astimezone(APP_TZ) if dt else None


def fmt_local(value: datetime | None, pattern: str = "%Y-%m-%d %H:%M") -> str:
    """转本地时区后格式化；空值返回空串，模板里用 `or '—'` 兜底。"""
    dt = to_local(value)
    return dt.strftime(pattern) if dt else ""


def iso_utc(value: datetime | None) -> str | None:
    """带 +00:00 的 ISO 串，供接口对外输出（客户端才不用猜时区）。"""
    dt = as_aware(value)
    return dt.isoformat() if dt else None
