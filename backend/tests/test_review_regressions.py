"""Repository review regressions; runnable with pytest or as an isolated CI script."""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ["CIB_DATA_DIR"] = tempfile.mkdtemp(prefix="cib-review-")
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcp"))

from app import tokens
from app.db import engine, init_db
from app.main import app
from app.models import Entity, Product, ProductSpec
from app.seed import seed
from cib_mcp import McpServer, serve
from fastapi.testclient import TestClient
from sqlmodel import Session, select


def _client():
    init_db()
    seed()
    c = TestClient(app)
    c.__enter__()
    c.post("/login", data={"username": "admin", "password": "AdminPass123"})
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/compare").text).group(1)
    with Session(engine) as s:
        raw, _ = tokens.create_token(s, name="review", scopes=["admin"])
        s.commit()
    return c, csrf, {"Authorization": f"Bearer {raw}"}


def test_cookie_writes_require_csrf_but_bearer_does_not():
    c, csrf, bearer = _client()
    body = {"name": "CSRF regression", "scopes": ["read"]}
    try:
        for headers in ({}, {"X-CSRF-Token": "wrong"}):
            r = c.post("/api/v1/tokens", json=body, headers=headers)
            assert r.status_code == 403, r.text
            assert r.json()["error"]["code"] == "FORBIDDEN"
        assert c.post("/api/v1/tokens", json=body, headers={"X-CSRF-Token": csrf}).status_code == 201
        assert c.post("/api/v1/tokens", json=body, headers=bearer).status_code == 201
    finally:
        c.__exit__(None, None, None)


def test_unicode_csrf_and_matrix_ids_fail_safely():
    c, _, bearer = _client()
    try:
        assert c.post("/logout", data={"_csrf": "非法令牌"}).status_code == 403
        for raw in ("²", "9" * 4500):
            assert c.get("/api/v1/matrix", params={"p": raw}, headers=bearer).status_code == 200
    finally:
        c.__exit__(None, None, None)


def test_routing_and_nonfinite_validation_use_api_envelope():
    c, _, bearer = _client()
    try:
        for path, status in (("/api/v1/nonexistent", 404), ("/api/v1/context", 405)):
            r = c.request("GET" if status == 404 else "POST", path, headers=bearer)
            assert r.status_code == status
            assert r.json()["ok"] is False
        r = c.post("/api/v1/tokens", content='{"name":"bad","expires_days":1e999}',
                   headers={**bearer, "Content-Type": "application/json"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    finally:
        c.__exit__(None, None, None)


def test_etag_covers_linked_fields_and_patch_matches_detail():
    c, _, bearer = _client()
    try:
        with Session(engine) as s:
            p = s.exec(select(Product)).first()
            pid, eid = p.id, p.entity_id
        path = f"/api/v1/products/{pid}"
        before = c.get(path, headers=bearer)
        with Session(engine) as s:
            e = s.get(Entity, eid)
            e.name += " review"
            s.add(e)
            s.commit()
        after = c.get(path, headers=bearer)
        assert after.headers["etag"] != before.headers["etag"]
        r = c.patch(path, json={"note": "review"}, headers=bearer)
        detail = c.get(path, headers=bearer)
        assert r.json()["data"] == detail.json()["data"]
        assert r.headers["etag"] == detail.headers["etag"]
    finally:
        c.__exit__(None, None, None)


def test_changes_cursor_advances_across_equal_timestamps():
    c, _, bearer = _client()
    try:
        stamp = datetime(2090, 1, 1, tzinfo=timezone.utc)
        with Session(engine) as s:
            rows = s.exec(select(ProductSpec).limit(3)).all()
            assert len(rows) == 3
            for row in rows:
                row.updated_at = stamp
                s.add(row)
            s.commit()
        query = {"since": stamp.isoformat(), "limit": 1}
        seen = set()
        for _ in range(3):
            body = c.get("/api/v1/changes", params=query, headers=bearer).json()
            item = body["data"][0]
            identity = (item["product_id"], item["spec"]["metric_key"])
            assert identity not in seen
            seen.add(identity)
            query["cursor"] = body["meta"]["next_cursor"]
        assert len(seen) == 3
        assert not c.get("/api/v1/changes", params=query, headers=bearer).json()["data"]
    finally:
        c.__exit__(None, None, None)


def test_token_expiry_is_bounded():
    c, _, bearer = _client()
    try:
        for days in (-1, 0, 10**12):
            r = c.post("/api/v1/tokens", json={"name": "bad expiry", "expires_days": days}, headers=bearer)
            assert r.status_code == 422, r.text
    finally:
        c.__exit__(None, None, None)


def test_mcp_invalid_messages_do_not_kill_stdio():
    server = McpServer(None)
    malformed = [
        {"jsonrpc": "2.0", "id": 1, "method": 42},
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": [1]},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": []}},
    ]
    out = io.StringIO()
    stream = io.StringIO("\n".join(json.dumps(m) for m in malformed + [
        {"jsonrpc": "2.0", "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
    ]))
    assert serve(server, stream, out) == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(replies) == 4
    assert all(r["error"]["code"] in (-32600, -32602) for r in replies[:3])
    assert replies[-1]["result"] == {}


def test_pdf_upload_extracts_text_and_rejects_corrupt_pdf():
    import pymupdf

    c, csrf, _ = _client()
    try:
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), "Air flow: 0.7 L/min")
            raw = pdf.tobytes()
        form = {"entity_name": "PDF review", "model": "PDF-REVIEW", "category": "pump_air"}
        headers = {"X-CSRF-Token": csrf}
        r = c.post("/api/datasheet/analyze", data=form,
                   files={"file": ("review.pdf", raw, "application/pdf")}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["chars"] >= len("Air flow: 0.7 L/min")
        assert any(row["value_num"] == 700 for row in r.json()["candidates"])
        r = c.post("/api/datasheet/analyze", data=form,
                   files={"file": ("broken.pdf", b"not a PDF", "application/pdf")}, headers=headers)
        assert r.status_code == 422, r.text
        with patch("app.routes_datasheet.os.replace", side_effect=OSError("disk unavailable")):
            r = c.post("/api/datasheet/analyze", data=form,
                       files={"file": ("review.txt", b"Air flow 0.9 L/min")}, headers=headers)
        assert r.status_code == 503, r.text
    finally:
        c.__exit__(None, None, None)


def test_exported_user_text_is_not_a_formula():
    from app.backup import export_all_xlsx
    from app.export_safety import csv_text
    from openpyxl import load_workbook

    c, _, _ = _client()
    try:
        formula = '=HYPERLINK("https://example.com","user text")'
        with Session(engine) as s:
            p = s.exec(select(Product)).first()
            old_model, old_note = p.model, p.note
            p.note = formula
            p.model = "XML\x01review"
            s.add(p)
            s.commit()
            try:
                content = export_all_xlsx(s)
            finally:
                p.model, p.note = old_model, old_note
                s.add(p)
                s.commit()
        wb = load_workbook(io.BytesIO(content), data_only=False)
        matches = [cell for ws in wb for row in ws for cell in row if cell.value == formula]
        assert matches and all(cell.data_type == "s" for cell in matches)
        assert any(cell.value == "XML�review" for ws in wb for row in ws for cell in row)
        wb.close()
        for value in (formula, "+SUM(1,2)", " @SUM(1,2)", "\t=1+1"):
            assert csv_text(value).startswith("'")
        assert csv_text("-40") == "-40"
    finally:
        c.__exit__(None, None, None)


def test_mcp_forwards_retry_and_version_headers():
    class ApiStub:
        def tool_manifest(self):
            return [{"name": "patch_product", "method": "PATCH", "path": "/api/v1/products/{product_id}"}]

        def call(self, method, path, **kwargs):
            assert method == "PATCH" and path == "/api/v1/products/1"
            assert kwargs["headers"] == {"Idempotency-Key": "retry-1", "If-Match": 'W/"version"'}
            assert kwargs["body"] == {"note": "changed"}
            return 200, {"ok": True, "data": {"id": 1}}

    response = McpServer(ApiStub()).handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "patch_product", "arguments": {
            "product_id": 1, "note": "changed", "idempotency_key": "retry-1", "if_match": 'W/"version"',
        }},
    })
    assert not response["result"]["isError"]


def test_units_overflow_and_multiple_values_keep_correct_scale():
    from app.importer import parse_cell
    from app.models import SpecMetric
    from app.units import convert

    assert not convert(1e308, "L/min", "mL/min").ok
    metric = SpecMetric(key="flow_test", name="flow", category="g", unit="mL/min")
    cell = parse_cell("0.7 L/min / 0.9 L/min", metric, "L/min")
    assert cell.review and cell.value_num == 700


def test_legacy_product_merge_preserves_adoptions_and_internal_spaces():
    from app import schema
    from app.models import Adoption
    from sqlalchemy import create_engine, event
    from sqlmodel import SQLModel

    path = Path(tempfile.mkdtemp(prefix="cib-migration-review-")) / "legacy.db"
    legacy = create_engine(f"sqlite:///{path.as_posix()}")

    @event.listens_for(legacy, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(legacy)
    with Session(legacy) as s:
        s.add(Entity(id=1, kind="competitor", name="owner"))
        s.add(Entity(id=2, kind="customer", name="customer"))
        s.commit()
        for pid, model in ((1, "A1"), (2, "a1 "), (3, "A 1")):
            s.add(Product(id=pid, entity_id=1, model=model))
        s.commit()
        s.add(Adoption(product_id=2, customer_id=2))
        s.commit()
    schema.migrate(legacy)
    with Session(legacy) as s:
        assert len(s.exec(select(Product)).all()) == 2
        adoption = s.exec(select(Adoption)).one()
        assert adoption.product_id == 1
    legacy.dispose()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    raise SystemExit(bool(failures))
