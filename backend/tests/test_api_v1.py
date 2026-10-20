"""AI 调用接口（/api/v1）的单测。

守的是「机器能可靠调用」这件事，不是「接口存在」：
  · 没有凭证必须 401，scope 不够必须 403，且都走统一信封
  · dry_run 必须真不落库
  · 幂等键重放不能写两遍
  · 批量写参数必须逐项回执（部分成功不能谎报全成）
  · If-Match 过期必须 409，防止覆盖别人的改动
  · 令牌 secret 含下划线也要能校验（曾经 split("_") 把这类令牌全判无效）

两种跑法都支持：pytest tests/ 或 python tests/test_api_v1.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="cib-v1-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import tokens  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product, ProductSpec  # noqa: E402
from app.seed import seed  # noqa: E402

_state: dict[str, object] = {}


def _admin() -> TestClient:
    """用 Cookie 会话的管理员客户端：签发令牌、做对照。"""
    if _state.get("admin"):
        return _state["admin"]  # type: ignore[return-value]
    init_db()
    seed()
    c = TestClient(app)
    c.__enter__()
    r = c.post("/login", data={"username": "admin", "password": "AdminPass123"},
               follow_redirects=False)
    assert r.status_code == 303
    _state["admin"] = c
    return c


def _token(scopes=("read", "write", "ingest")) -> str:
    admin = _admin()
    r = admin.post("/api/v1/tokens", json={"name": "测试令牌", "scopes": list(scopes)})
    assert r.status_code == 201, r.text
    return r.json()["data"]["token"]


def _h(scopes=("read", "write", "ingest")) -> dict:
    return {"Authorization": f"Bearer {_token(scopes)}"}


def _product_id(model: str) -> int:
    with Session(engine) as s:
        p = s.exec(select(Product).where(Product.model == model)).first()
        assert p is not None, f"找不到 {model}"
        return p.id  # type: ignore[return-value]


# ── 鉴权与信封 ─────────────────────────────────────────────────────────────
def test_missing_credentials_is_401_envelope():
    c = TestClient(app)
    c.__enter__()
    r = c.get("/api/v1/products")
    assert r.status_code == 401
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "UNAUTHENTICATED"
    assert "hint" in body["error"], "错误要给出怎么改"


def test_invalid_token_is_401():
    c = TestClient(app)
    c.__enter__()
    r = c.get("/api/v1/context", headers={"Authorization": "Bearer cib_dead_beef"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


def test_read_only_token_cannot_write():
    h = _h(("read",))
    c = TestClient(app)
    c.__enter__()
    r = c.post("/api/v1/products",
               json={"entity_id": 2, "model": "RO-1", "category": "pump_air"}, headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"
    assert "read" in r.json()["error"]["hint"]

    r = c.post("/api/v1/import", json={"rows": []}, headers=h)
    assert r.status_code == 403, "ingest 也要拦"


def test_token_secret_with_underscore_is_valid():
    """urlsafe base64 会产出下划线；按 '_' 全拆会把这类令牌判无效。"""
    with patch.object(tokens.secrets, "token_urlsafe", return_value="abc_def-ghi"):
        with Session(engine) as s:
            raw, _row = tokens.create_token(s, name="下划线令牌", scopes="read")
            assert raw.endswith("abc_def-ghi")
            assert tokens.verify(s, raw) is not None, "含下划线的 secret 也必须能校验"


def test_validation_error_uses_envelope():
    c = TestClient(app)
    c.__enter__()
    r = c.get("/api/v1/products?limit=999", headers=_h())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert r.json()["error"].get("details")


def test_old_routes_still_work():
    """v1 是新增层，页面路由一条未动。"""
    admin = _admin()
    for path in ("/compare", "/products", "/import", "/datasheet"):
        assert admin.get(path).status_code == 200, path


# ── 查询面 ─────────────────────────────────────────────────────────────────
def test_context_and_listing():
    c = TestClient(app)
    c.__enter__()
    h = _h()
    d = c.get("/api/v1/context", headers=h).json()["data"]
    assert d["counts"]["products"] >= 11
    assert any(m["key"] == "flow_air" for m in d["metric_keys"])

    r = c.get("/api/v1/products?category=pump_air&limit=2", headers=h).json()
    assert len(r["data"]) == 2
    assert r["data"][0]["category"] == "pump_air"
    assert "completeness" in r["data"][0]
    assert r["meta"]["next_cursor"], "还有下一页时给游标"


def test_product_detail_has_etag_and_missing():
    c = TestClient(app)
    c.__enter__()
    pid = _product_id("MZB1001T02")
    r = c.get(f"/api/v1/products/{pid}", headers=_h())
    assert r.status_code == 200
    assert r.headers.get("etag", "").startswith('W/"')
    d = r.json()["data"]
    assert d["specs"]
    assert d["specs_filled"] == len(d["specs"])
    assert isinstance(d["missing_metrics"], list)


def test_matrix_matches_page_semantics():
    c = TestClient(app)
    c.__enter__()
    pid = _product_id("MZB1001T02")
    r = c.get(f"/api/v1/matrix?p={pid}", headers=_h()).json()["data"]
    assert r["rows"], "矩阵不应为空"
    row = r["rows"][0]
    assert {"metric_key", "values", "differs", "has_missing"} <= set(row)


def test_changes_feed():
    c = TestClient(app)
    c.__enter__()
    r = c.get("/api/v1/changes?since=2000-01-01T00:00:00Z&limit=5", headers=_h())
    assert r.status_code == 200
    assert len(r.json()["data"]) <= 5
    assert r.json()["meta"].get("next_since")

    bad = c.get("/api/v1/changes?since=昨天", headers=_h())
    assert bad.status_code == 422
    assert bad.json()["error"]["field"] == "since"


# ── 写入面 ─────────────────────────────────────────────────────────────────
def test_dry_run_does_not_persist():
    c = TestClient(app)
    c.__enter__()
    h = _h()
    r = c.post("/api/v1/products",
               json={"entity_id": 2, "model": "DRY-77", "category": "pump_air",
                     "dry_run": True}, headers=h)
    assert r.status_code == 200
    assert r.json()["meta"]["dry_run"] is True
    assert not c.get("/api/v1/products?q=DRY-77", headers=h).json()["data"], "预演必须不落库"

    with Session(engine) as s:
        assert s.exec(select(Product).where(Product.model == "DRY-77")).first() is None


def test_idempotency_replay_writes_once():
    c = TestClient(app)
    c.__enter__()
    h = {**_h(), "Idempotency-Key": "create-IDEM-1"}
    body = {"entity_id": 2, "model": "IDEM-1", "category": "pump_air"}
    r1 = c.post("/api/v1/products", json=body, headers=h)
    r2 = c.post("/api/v1/products", json=body, headers=h)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r2.json()["meta"].get("replayed") is True
    with Session(engine) as s:
        rows = s.exec(select(Product).where(Product.model == "IDEM-1")).all()
    assert len(rows) == 1, f"重放写了两遍：{len(rows)}"


def test_duplicate_model_returns_409_with_hint():
    c = TestClient(app)
    c.__enter__()
    h = _h()
    c.post("/api/v1/products",
           json={"entity_id": 2, "model": "DUP-1", "category": "pump_air"}, headers=h)
    r = c.post("/api/v1/products",
               json={"entity_id": 2, "model": "dup-1", "category": "pump_air"}, headers=h)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "MODEL_EXISTS"
    assert err["details"]["product_id"]


def test_specs_batch_partial_success_is_explicit():
    c = TestClient(app)
    c.__enter__()
    h = _h()
    pid = _product_id("MZB3004T04")
    r = c.post("/api/v1/specs/batch", json={"items": [
        {"product_id": pid, "metric_key": "flow_air", "value_num": 0.2,
         "condition": "@18Vdc", "verified": True},
        {"product_id": pid, "metric_key": "flow_liquid", "value_num": 1},
        {"product_id": pid, "metric_key": "no_such_metric", "value_num": 1},
    ]}, headers=h)
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["applied"] == 1
    assert r.json()["meta"]["failed"] == 2
    codes = [x.get("code") for x in d["results"]]
    assert "CATEGORY_CONFLICT" in codes and "VALIDATION_FAILED" in codes

    with Session(engine) as s:
        spec = s.exec(select(ProductSpec).where(ProductSpec.product_id == pid)).all()
    assert any(abs(x.value_num - 0.2) < 1e-9 for x in spec if x.value_num is not None)


def test_delete_requires_confirm():
    c = TestClient(app)
    c.__enter__()
    h = _h()
    pid = _product_id("mp6-pp")
    r = c.delete(f"/api/v1/products/{pid}", headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CONFIRM_REQUIRED"
    with Session(engine) as s:
        assert s.get(Product, pid) is not None, "未确认时不得删除"


def test_etag_guard_prevents_overwrite():
    c = TestClient(app)
    c.__enter__()
    h = _h()
    pid = _product_id("mp6-gas")
    etag = c.get(f"/api/v1/products/{pid}", headers=h).headers["etag"]

    stale = c.patch(f"/api/v1/products/{pid}", json={"name": "旧"}, headers={**h, "If-Match": '"stale"'})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONFLICT_VERSION"

    ok = c.patch(f"/api/v1/products/{pid}", json={"name": "新名字"},
                 headers={**h, "If-Match": etag})
    assert ok.status_code == 200 and ok.json()["data"]["name"] == "新名字"


def test_category_change_blocked_when_specs_exist():
    c = TestClient(app)
    c.__enter__()
    h = _h()
    pid = _product_id("mp6-gas")
    r = c.patch(f"/api/v1/products/{pid}", json={"category": "fan"}, headers=h)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CATEGORY_CONFLICT"


def test_import_accepts_native_json_types():
    """JSON 调用方会传数字而不是字符串，不能因此崩。"""
    c = TestClient(app)
    c.__enter__()
    h = _h()
    rows = [
        {"型号": "JSN-1", "友商": "JSON 导入测试", "大类": "压电气泵", "指标": "静压", "数值": 3.5},
        {"型号": "JSN-1", "友商": "JSON 导入测试", "大类": "压电气泵", "指标": "瞎写", "数值": 1},
    ]
    r = c.post("/api/v1/import", json={"rows": rows}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["written_specs"] == 1
    assert d["errors"], "坏行要报出来"

    pid = c.get("/api/v1/products?q=JSN-1", headers=h).json()["data"][0]["id"]
    spec = c.get(f"/api/v1/products/{pid}/specs", headers=h).json()["data"][0]
    assert abs(spec["value_num"] - 3.5) < 1e-9


def test_import_dry_run_rolls_back_everything():
    c = TestClient(app)
    c.__enter__()
    h = _h()
    rows = [{"型号": "JSN-DRY", "友商": "不该出现的公司", "大类": "压电气泵",
             "指标": "静压", "数值": 1}]
    r = c.post("/api/v1/import", json={"rows": rows, "dry_run": True}, headers=h)
    assert r.json()["data"]["written_specs"] == 1
    assert not c.get("/api/v1/products?q=JSN-DRY", headers=h).json()["data"]
    ents = c.get("/api/v1/entities", headers=h).json()["data"]
    assert not any(e["name"] == "不该出现的公司" for e in ents), "预演连公司也不能建"


# ── 发现面 ─────────────────────────────────────────────────────────────────
def test_tools_manifest_is_usable():
    c = TestClient(app)
    c.__enter__()
    d = c.get("/api/v1/tools", headers=_h()).json()["data"]
    names = {t["name"] for t in d["tools"]}
    assert {"get_context", "search_products", "write_specs", "create_product"} <= names
    write_specs = next(t for t in d["tools"] if t["name"] == "write_specs")
    assert write_specs["parameters"]["type"] == "object"
    assert any("dry_run" in c for c in d["conventions"]), "约定里要提醒先预演"


def test_token_management_api():
    admin = _admin()
    r = admin.post("/api/v1/tokens", json={"name": "待吊销", "scopes": ["read"]})
    tid = r.json()["data"]["id"]
    assert admin.delete(f"/api/v1/tokens/{tid}").status_code == 200
    assert admin.delete(f"/api/v1/tokens/{tid}").status_code == 404
    listed = admin.get("/api/v1/tokens").json()["data"]
    assert any(t["id"] == tid and t["revoked"] for t in listed)


# ── 运行器 ─────────────────────────────────────────────────────────────────
def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}  {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}  {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
