"""结构化日志：一秒一行 JSON，带请求 ID，便于采集与问题定位。

设计取舍：
- 不用 uvicorn 默认 access log：它只有 IP/方法/路径/状态，没有请求 ID，
  也无法把同一次请求里的业务日志串起来；这里用一个中间件统一记录。
- 请求 ID 优先复用上游传来的 X-Request-ID（仅接受 1~64 位安全字符），
  否则生成 12 位随机串；同时回写到响应头，用户报错时能直接对上日志。
- 输出一行一个 JSON 对象，字段固定：ts / level / logger / msg，
  以及可选的 request_id / event / method / path / status / duration_ms /
  client / user / role / detail。异常额外带 exc_type / exc_message / exc_stack。
- CIB_LOG_FORMAT=plain 可切回人类可读格式（本地开发用），默认 json。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

# 当前请求的 ID。放在 contextvar 里，业务代码不必层层传参就能带上请求上下文。
_REQUEST_ID: ContextVar[str] = ContextVar("cib_request_id", default="")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# 允许从 LogRecord 透传到 JSON 的自定义字段（其他 extra 一律忽略，避免把
# 任意对象塞进日志导致序列化失败或泄露请求体）。
_EXTRA_FIELDS = (
    "event",
    "method",
    "path",
    "status",
    "duration_ms",
    "client",
    "user",
    "role",
    "detail",
)

_configured = False
log = logging.getLogger("cib.request")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def normalize_request_id(raw: str | None) -> str:
    """只接受上游传来的安全字符；非法值返回空串，由调用方生成新 ID。"""
    value = (raw or "").strip()
    return value if _REQUEST_ID_RE.match(value) else ""


def current_request_id() -> str:
    return _REQUEST_ID.get()


def set_request_id(value: str) -> Token[str]:
    return _REQUEST_ID.set(value)


def reset_request_id(token: Token[str]) -> None:
    _REQUEST_ID.reset(token)


class JsonFormatter(logging.Formatter):
    """把 LogRecord 压成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        rid = getattr(record, "request_id", "") or current_request_id()
        if rid:
            payload["request_id"] = rid
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        payload["msg"] = record.getMessage()
        for key in _EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            exc_type, exc, _tb = record.exc_info
            if exc_type is not None:
                payload["exc_type"] = exc_type.__name__
            if exc is not None:
                payload["exc_message"] = str(exc)
            payload["exc_stack"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


class PlainFormatter(logging.Formatter):
    """本地开发用的可读格式；仍然是同一套字段。"""

    def format(self, record: logging.LogRecord) -> str:
        rid = getattr(record, "request_id", "") or current_request_id()
        prefix = f"[{rid}] " if rid else ""
        event = getattr(record, "event", "")
        event_part = f" ({event})" if event else ""
        text = (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} "
            f"{record.levelname:<5} {record.name} {prefix}{record.getMessage()}{event_part}"
        )
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def configure() -> None:
    """进程级配置：root 一个 handler；关掉 uvicorn 自带的 access log。

    uvicorn 在导入 app 之前已经配置过自己的 logger，这里显式接管
    uvicorn/uvicorn.error，避免同一条日志被写两遍、或一半 JSON 一半文本。
    """
    global _configured
    if _configured:
        return

    level_name = os.getenv("CIB_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.getenv("CIB_LOG_FORMAT", "json").strip().lower()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(PlainFormatter() if fmt == "plain" else JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(level)

    # 访问日志交给本模块的中间件统一输出，避免 uvicorn 的文本格式混进来。
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True

    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def install(app: Any) -> None:
    """给 FastAPI 安装请求 ID + 访问日志中间件。"""

    @app.middleware("http")
    async def _request_log_middleware(request, call_next):
        raw = request.headers.get("x-request-id")
        rid = normalize_request_id(raw) or new_request_id()
        token = set_request_id(rid)
        # 让模板和异常处理器都能拿到同一个 ID（页脚展示、API meta 使用）。
        request.state.request_id = rid

        path = request.url.path
        method = request.method
        client = request.client.host if request.client else None
        quiet = path.startswith("/static/") or path == "/favicon.ico"
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = round((time.perf_counter() - started) * 1000, 1)
            log.exception(
                "%s %s 未处理异常",
                method,
                path,
                extra={
                    "event": "http_request",
                    "request_id": rid,
                    "method": method,
                    "path": path,
                    "status": 500,
                    "duration_ms": duration,
                    "client": client,
                    "user": getattr(request.state, "user", None),
                    "role": getattr(request.state, "role", None),
                },
            )
            raise
        else:
            status = response.status_code
            level = (
                logging.ERROR
                if status >= 500
                else logging.WARNING
                if status >= 400
                else logging.INFO
            )
            if not (quiet and level < logging.WARNING):
                duration = round((time.perf_counter() - started) * 1000, 1)
                log.log(
                    level,
                    "%s %s %d",
                    method,
                    path,
                    status,
                    extra={
                        "event": "http_request",
                        "request_id": rid,
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_ms": duration,
                        "client": client,
                        "user": getattr(request.state, "user", None),
                        "role": getattr(request.state, "role", None),
                    },
                )
            # 回写响应头：curl -i / 浏览器网络面板都能直接看到这个 ID。
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            reset_request_id(token)