"""客户采用关系：谁在用哪款泵。

这一组守的规则：
  · 一条记录 = 一家客户 × 一款型号；同客户同型号重复添加要被挡住，而不是并排两条
  · 客户不存在时按名字建档（kind=customer），同名（忽略空格标点大小写）复用，不新建
  · 状态只能是五档之一（传闻/送样/导入设计/量产/已停用），乱填报错不落库
  · 增删都留审计，label 写「客户 · 型号」，人看得懂
  · 渠道一致：页面路由与 /api/v1/adoptions 走同一套校验

两种跑法都支持：pytest tests/ 或 python tests/test_adoptions.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

_TMP = tempfile.mkdtemp(prefix="cib-adoption-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine, init_db
from app.main import app
from app.models import Adoption, AuditLog, Entity, Product
from app.seed import seed
from fastapi.testclient import TestClient
from sqlmodel import Session, select

_state: dict[str, object] = {}


def _client() -> TestClient:
    if _state.get("client"):
        return _state["client"]  # type: ignore[return-value]
    init_db()
    seed()
    c = TestClient(app)
    c.__enter__()
    r = c.post("/login", data={"username": "admin", "password": "AdminPass123"},
               follow_redirects=False)
    assert r.status_code == 303, f"登录失败：{r.status_code}"
    _state["client"] = c
    return c


def _csrf(c: TestClient, path: str) -> str:
    html = c.get(path).text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, f"{path} 没有 CSRF 令牌"
    return m.group(1)


def _product(model: str) -> Product:
    with Session(engine) as s:
        p = s.exec(select(Product).where(Product.model == model)).first()
        assert p is not None, f"找不到型号 {model}"
        return p


def _token(c: TestClient, scopes=("read", "write")) -> dict:
    key = "tok_" + ",".join(scopes)
    if key not in _state:
        r = c.post("/api/v1/tokens", json={"name": "采用测试", "scopes": list(scopes)})
        assert r.status_code == 201, r.text
        _state[key] = {"Authorization": f"Bearer {r.json()['data']['token']}"}
    return _state[key]  # type: ignore[return-value]


def _adoptions(product_id: int) -> list[Adoption]:
    with Session(engine) as s:
        return s.exec(
            select(Adoption).where(Adoption.product_id == product_id)
        ).all()


# ── 页面通道 ───────────────────────────────────────────────────────────────
def test_page_add_adoption_creates_customer_and_audit():
    c = _client()
    p = _product("MZB1001T02")
    csrf = _csrf(c, f"/products/{p.id}")

    r = c.post(f"/products/{p.id}/adoptions",
               data={"customer_name": "测试客户甲", "status": "mass_production",
                     "year": "2025", "note": "用在血压计气路", "_csrf": csrf},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    assert "已记录" in unquote(r.headers["location"]), unquote(r.headers["location"])

    rows = _adoptions(p.id)
    assert len(rows) == 1
    assert rows[0].status == "mass_production" and rows[0].year == 2025
    with Session(engine) as s:
        cust = s.get(Entity, rows[0].customer_id)
        assert cust is not None and cust.kind == "customer", "新客户要建成 kind=customer"
        log = s.exec(select(AuditLog).where(AuditLog.object_type == "adoption")).all()
        assert log and "测试客户甲" in log[-1].object_label


def test_page_rejects_duplicate_customer_for_same_product():
    """同一客户 + 同一型号只能有一条：状态变了是「改那一条」，不是再记一条。

    这条自带前置，不依赖别的用例先建过数据（测试间共享库，顺序变了就会假失败）。
    """
    c = _client()
    p = _product("MZB3005T06")
    csrf = _csrf(c, f"/products/{p.id}")
    first = c.post(f"/products/{p.id}/adoptions",
                   data={"customer_name": "重复客户", "status": "sample", "_csrf": csrf},
                   follow_redirects=False)
    assert "已记录" in unquote(first.headers["location"])

    again = c.post(f"/products/{p.id}/adoptions",
                   data={"customer_name": "重复客户", "status": "mass_production", "_csrf": csrf},
                   follow_redirects=False)
    assert again.status_code == 303
    assert "已有记录" in unquote(again.headers["location"]), unquote(again.headers["location"])
    assert len(_adoptions(p.id)) == 1, "重复添加不能并排写入两条"


def test_page_reuses_existing_customer_by_normalized_name():
    """「测试客户甲」与「测试客户甲 」是同一家 —— 归一后复用，别再建一份档案。"""
    c = _client()
    p = _product("MZB3004T04")
    csrf = _csrf(c, f"/products/{p.id}")
    with Session(engine) as s:
        before = len([e for e in s.exec(select(Entity)).all() if e.kind == "customer"])
    r = c.post(f"/products/{p.id}/adoptions",
               data={"customer_name": " 测试客户甲 ", "status": "sample", "_csrf": csrf},
               follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        after = len([e for e in s.exec(select(Entity)).all() if e.kind == "customer"])
    assert after == before, "同名客户应复用，不该多一份档案"


def test_page_rejects_bad_status_and_year():
    c = _client()
    p = _product("MZB3005T06")
    csrf = _csrf(c, f"/products/{p.id}")
    bad_status = c.post(f"/products/{p.id}/adoptions",
                        data={"customer_name": "测试客户乙", "status": "量产中", "_csrf": csrf},
                        follow_redirects=False)
    assert "不可识别" in unquote(bad_status.headers["location"])

    bad_year = c.post(f"/products/{p.id}/adoptions",
                      data={"customer_name": "测试客户乙", "status": "sample",
                            "year": "去年", "_csrf": csrf}, follow_redirects=False)
    assert "不是数字" in unquote(bad_year.headers["location"])
    assert _adoptions(p.id) == []


def test_page_delete_adoption():
    c = _client()
    p = _product("MZB1001T02")
    csrf = _csrf(c, f"/products/{p.id}")
    row = _adoptions(p.id)[0]
    r = c.post(f"/products/{p.id}/adoptions/{row.id}/delete",
               data={"_csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert _adoptions(p.id) == []
    with Session(engine) as s:
        assert s.exec(
            select(AuditLog).where(AuditLog.action == "delete",
                                   AuditLog.object_type == "adoption")
        ).first() is not None, "删除也要留痕"


def test_viewer_cannot_touch_adoptions():
    c = _client()
    p = _product("MZB1001T02")
    csrf = _csrf(c, "/admin/users")
    c.post("/admin/users/create", data={
        "username": "viewer_adopt", "password": "ViewerPass123",
        "display_name": "只读", "role": "viewer", "_csrf": csrf,
    }, follow_redirects=False)

    v = TestClient(app)
    v.__enter__()
    v.post("/login", data={"username": "viewer_adopt", "password": "ViewerPass123"},
           follow_redirects=False)
    r = v.post(f"/products/{p.id}/adoptions",
               data={"customer_name": "越权客户", "status": "sample", "_csrf": "x"},
               follow_redirects=False)
    assert r.status_code == 403, f"只读账号不能写采用关系，实际 {r.status_code}"
    assert _adoptions(p.id) == []


# ── API 通道 ───────────────────────────────────────────────────────────────
def test_api_create_list_patch_adoption():
    c = _client()
    h = _token(c)
    p = _product("mp6-gas")

    r = c.post("/api/v1/adoptions",
               json={"product_id": p.id, "customer_name": "API 客户", "status": "sample"},
               headers=h)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["customer"] == "API 客户" and data["status_label"] == "送样"

    listed = c.get(f"/api/v1/adoptions?product_id={p.id}", headers=h).json()["data"]
    assert [x["id"] for x in listed] == [data["id"]]

    patched = c.patch(f"/api/v1/adoptions/{data['id']}",
                      json={"status": "mass_production", "year": 2026}, headers=h)
    assert patched.status_code == 200
    assert patched.json()["data"]["status"] == "mass_production"
    assert patched.json()["data"]["year"] == 2026


def test_api_duplicate_adoption_is_409():
    c = _client()
    h = _token(c)
    p = _product("mp6-gas+")
    for _ in range(1):
        c.post("/api/v1/adoptions",
               json={"product_id": p.id, "customer_name": "重复 API 客户", "status": "sample"},
               headers=h)
    r = c.post("/api/v1/adoptions",
               json={"product_id": p.id, "customer_name": "重复 API 客户", "status": "sample"},
               headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "ADOPTION_EXISTS"


def test_api_rejects_unknown_status_and_product():
    c = _client()
    h = _token(c)
    p = _product("mp6-gas")
    bad = c.post("/api/v1/adoptions",
                 json={"product_id": p.id, "customer_name": "X", "status": "量产中"},
                 headers=h)
    assert bad.status_code == 422, bad.text
    assert bad.json()["error"]["code"] == "VALIDATION_FAILED"

    missing = c.post("/api/v1/adoptions",
                     json={"product_id": 999999, "customer_name": "X"}, headers=h)
    assert missing.status_code == 404


def test_api_product_detail_includes_adoptions():
    """型号详情要带采用关系 —— 否则 AI 得先查型号再单独查一次采用。"""
    c = _client()
    h = _token(c)
    p = _product("mp6-pi")
    created = c.post("/api/v1/adoptions",
                     json={"product_id": p.id, "customer_name": "详情客户",
                           "status": "design_in"}, headers=h)
    assert created.status_code == 201, created.text
    body = c.get(f"/api/v1/products/{p.id}", headers=h).json()["data"]
    assert "adoptions" in body
    assert [a["customer"] for a in body["adoptions"]] == ["详情客户"]
    assert body["adoptions"][0]["status_label"] == "导入设计"


def test_api_delete_adoption():
    c = _client()
    h = _token(c)
    p = _product("mp6-gas")
    rows = c.get(f"/api/v1/adoptions?product_id={p.id}", headers=h).json()["data"]
    assert rows
    r = c.delete(f"/api/v1/adoptions/{rows[0]['id']}", headers=h)
    assert r.status_code == 200
    assert c.get(f"/api/v1/adoptions?product_id={p.id}", headers=h).json()["data"] == []


def test_api_rejects_self_adoption():
    """厂商自己的型号被自己「采用」没有意义，会污染「谁在用这款泵」。"""
    c = _client()
    h = _token(c)
    p = _product("mp6-pp")
    r = c.post("/api/v1/adoptions",
               json={"product_id": p.id, "customer_name": "巴特尔斯微技术",
                     "status": "sample"}, headers=h)
    assert r.status_code == 422, r.text
    assert "厂商自己" in r.json()["error"]["message"]


def test_api_rejects_out_of_range_year():
    """年份范围在接口与页面必须同一套判断（曾经只有页面校验）。"""
    c = _client()
    h = _token(c)
    p = _product("mp6-pp")
    r = c.post("/api/v1/adoptions",
               json={"product_id": p.id, "customer_name": "年份测试客户",
                     "status": "sample", "year": 1500}, headers=h)
    assert r.status_code == 422, r.text
    assert "2000" in r.json()["error"]["message"]

    listed = c.get(f"/api/v1/adoptions?product_id={p.id}", headers=h).json()["data"]
    assert listed == [], "被拒的记录不能落库"


def test_etag_changes_when_adoption_is_added():
    """ETag 必须覆盖响应体里的所有字段 —— 加了采用记录却不变，条件请求会拿到旧数据。"""
    c = _client()
    h = _token(c)
    p = _product("mp6-liq")
    first = c.get(f"/api/v1/products/{p.id}", headers=h)
    etag_before = first.headers["ETag"]

    created = c.post("/api/v1/adoptions",
                     json={"product_id": p.id, "customer_name": "ETag 客户",
                           "status": "sample"}, headers=h)
    assert created.status_code == 201, created.text

    second = c.get(f"/api/v1/products/{p.id}", headers=h)
    assert second.headers["ETag"] != etag_before, "采用记录变了，ETag 也该变"

    # 条件请求：拿旧 ETag 应当拿到 200 + 新内容，而不是 304（304 需要服务端自己实现，
    # 这里至少保证 ETag 已变，客户端不会误判）
    assert second.status_code == 200
    assert [a["customer"] for a in second.json()["data"]["adoptions"]] == ["ETag 客户"]


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
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}  {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
