"""结构化日志与页脚版本号。"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-log-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_PASSWORD"] = "{{SECRET_5m2vuxpq}}"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import logging_setup
from app.main import app
from fastapi.testclient import TestClient

_ADMIN_PW = "{{SECRET_5m2vuxpq}}"


def _client() -> TestClient:
    c = TestClient(app)
    c.__enter__()
    return c


def test_json_formatter_has_stable_fields():
    """一行一个 JSON；request_id / event / 状态码 / 耗时都在固定字段里。"""
    record = logging.LogRecord("cib.test", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
    record.event = "http_request"
    record.request_id = "trace-1"
    record.method = "GET"
    record.path = "/news"
    record.status = 404
    record.duration_ms = 1.25
    payload = json.loads(logging_setup.JsonFormatter().format(record))

    assert payload["level"] == "warning"
    assert payload["logger"] == "cib.test"
    assert payload["msg"] == "hello world"
    assert payload["request_id"] == "trace-1"
    assert payload["event"] == "http_request"
    assert payload["method"] == "GET"
    assert payload["path"] == "/news"
    assert payload["status"] == 404
    assert payload["duration_ms"] == 1.25
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", payload["ts"])


def test_request_id_is_reused_from_header_and_echoed():
    """上游带来的安全 X-Request-ID 必须被复用，并回写到响应头与页脚。"""
    c = _client()
    r = c.get("/login", headers={"X-Request-ID": "trace-abc.123"})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "trace-abc.123"
    assert "trace-abc.123" in r.text
    assert "请求 ID" in r.text
    assert f"v{app.version}" in r.text


def test_invalid_request_id_is_replaced_with_generated_one():
    """空格/尖括号之类的非法值不能原样进日志，也不能回写响应头。"""
    c = _client()
    r = c.get("/login", headers={"X-Request-ID": "bad id <script>"})
    rid = r.headers["x-request-id"]
    assert rid != "bad id <script>"
    assert re.fullmatch(r"[0-9a-f]{12}", rid)
    assert rid in r.text


def test_request_log_contains_status_duration_and_identity(caplog):
    """访问日志要有方法、路径、状态码、耗时，以及登录后的用户。"""
    c = _client()
    c.post("/login", data={"username": "admin", "password": _ADMIN_PW},
           follow_redirects=False)
    caplog.set_level(logging.INFO, logger="cib.request")
    r = c.get("/news", headers={"X-Request-ID": "trace-200"})
    assert r.status_code == 200

    hits = [
        rec for rec in caplog.records
        if getattr(rec, "event", "") == "http_request" and getattr(rec, "path", "") == "/news"
    ]
    assert hits, "没有记录 /news 的访问日志"
    rec = hits[-1]
    assert rec.method == "GET"
    assert rec.status == 200
    assert rec.duration_ms >= 0
    assert rec.request_id == "trace-200"
    assert rec.user == "admin"
    assert rec.role == "admin"


def test_api_v1_envelope_uses_the_same_request_id():
    """/api/v1 响应里的 request_id 必须和访问日志/响应头一致。"""
    c = _client()
    r = c.get("/api/v1/health", headers={"X-Request-ID": "trace-api"})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "trace-api"
    assert r.json()["meta"]["request_id"] == "trace-api"


def test_not_found_page_shows_request_id_in_footer():
    c = _client()
    c.post("/login", data={"username": "admin", "password": _ADMIN_PW},
           follow_redirects=False)
    r = c.get("/products/99999", headers={"X-Request-ID": "trace-404", "Accept": "text/html"})
    assert r.status_code == 404
    assert "trace-404" in r.text
    assert "请求 ID" in r.text


def main() -> int:
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    main()