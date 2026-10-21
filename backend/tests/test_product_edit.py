"""产品数据编辑的单测：管理员能改，普通用户不能。

这一组守的是「管理员能改数据」这条基本能力，以及几条不该被绕过的规则：
  · 清空数值 = 删除该项（不留「有行无值」的脏数据）
  · 数值型指标填了非数字：不静默丢弃，也不覆盖旧值，要明确报错
  · 已有参数的型号不能换大类（会让历史参数落到错误的指标体系）
  · viewer 一律 403
  · 公司列表分两份：浏览用的筛选不含 0 型号的公司，新建用的下拉保留（否则建不出第一个型号）

两种跑法都支持：pytest tests/ 或 python tests/test_product_edit.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

_TMP = tempfile.mkdtemp(prefix="cib-edit-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine, init_db
from app.main import app
from app.models import Product, ProductSpec, SpecMetric
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
    assert r.status_code == 303
    _state["client"] = c
    return c


def _csrf(c: TestClient, path: str = "/products") -> str:
    html = c.get(path).text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, f"{path} 没有 CSRF 令牌"
    return m.group(1)


def _product_by_model(model: str) -> Product:
    with Session(engine) as s:
        p = s.exec(select(Product).where(Product.model == model)).first()
        assert p is not None, f"找不到型号 {model}"
        return p


def _metric(key: str) -> SpecMetric:
    with Session(engine) as s:
        m = s.exec(select(SpecMetric).where(SpecMetric.key == key)).first()
        assert m is not None
        return m


def _spec(product_id: int, metric_id: int):
    with Session(engine) as s:
        return s.exec(
            select(ProductSpec).where(
                ProductSpec.product_id == product_id,
                ProductSpec.metric_id == metric_id,
            )
        ).first()


# ── 页面可达 ───────────────────────────────────────────────────────────────
def test_pages_render_for_admin():
    c = _client()
    assert c.get("/products").status_code == 200
    assert c.get("/import").status_code == 200
    p = _product_by_model("MZB1001T02")
    r = c.get(f"/products/{p.id}")
    assert r.status_code == 200
    assert "MZB1001T02" in r.text
    assert "参数值" in r.text


def test_compare_page_links_to_edit():
    c = _client()
    html = c.get("/compare").text
    assert "/products/" in html, "对比页应给出编辑入口"


# ── 编辑参数 ───────────────────────────────────────────────────────────────
def test_edit_value_and_mark_verified():
    c = _client()
    p = _product_by_model("MZB3004T04")
    flow = _metric("flow_air")
    csrf = _csrf(c, f"/products/{p.id}")

    r = c.post(
        f"/products/{p.id}/specs",
        data={f"{flow.id}:v": "0.166", f"{flow.id}:c": "@18Vdc", f"{flow.id}:ok": "on", "_csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert "已保存" in unquote(r.headers["location"]), unquote(r.headers["location"])

    spec = _spec(p.id, flow.id)
    assert spec is not None
    assert abs(spec.value_num - 0.166) < 1e-9
    assert spec.condition == "@18Vdc"
    assert spec.verified is True
    assert spec.source_id is not None, "人工改动必须留来源"


def test_clearing_value_deletes_the_spec():
    c = _client()
    p = _product_by_model("MZB3004T04")
    flow = _metric("flow_air")
    assert _spec(p.id, flow.id) is not None, "前置条件：该项原本有值"
    csrf = _csrf(c, f"/products/{p.id}")

    r = c.post(
        f"/products/{p.id}/specs",
        data={f"{flow.id}:v": "", f"{flow.id}:c": "", "_csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _spec(p.id, flow.id) is None, "清空数值应删除该项，而不是留一行空值"


def test_invalid_number_keeps_old_value_and_reports():
    c = _client()
    p = _product_by_model("MZB1001T02")
    flow = _metric("flow_air")
    before = _spec(p.id, flow.id).value_num
    csrf = _csrf(c, f"/products/{p.id}")

    r = c.post(
        f"/products/{p.id}/specs",
        data={f"{flow.id}:v": "很大", "_csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "err=" in unquote(r.headers["location"]), "应带错误信息跳回"
    after = _spec(p.id, flow.id)
    assert after is not None and after.value_num == before, "非法输入不得覆盖旧值"


def test_add_value_for_previously_missing_metric():
    c = _client()
    p = _product_by_model("MZB1001T02")
    power = _metric("power_mw")
    assert _spec(p.id, power.id) is None, "前置条件：该型号未公开功耗"
    csrf = _csrf(c, f"/products/{p.id}")

    r = c.post(
        f"/products/{p.id}/specs",
        data={f"{power.id}:v": "18", f"{power.id}:c": "@15Vpp", "_csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    spec = _spec(p.id, power.id)
    assert spec is not None and spec.value_num == 18
    assert spec.verified is False, "未勾选复核时应保持未复核"


# ── 元信息 ─────────────────────────────────────────────────────────────────
def test_meta_update():
    c = _client()
    p = _product_by_model("mp6-gas+")
    csrf = _csrf(c, f"/products/{p.id}")
    r = c.post(
        f"/products/{p.id}",
        data={
            "model": "mp6-gas+", "name": "mp6-gas+ 高压版", "category": "pump_air",
            "status": "eol", "datasheet_url": "https://example.com/mp6.pdf", "note": "已停产",
            "_csrf": csrf,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(engine) as s:
        p2 = s.get(Product, p.id)
        assert p2.name == "mp6-gas+ 高压版"
        assert p2.status == "eol"
        assert p2.datasheet_url == "https://example.com/mp6.pdf"


def test_category_change_blocked_when_specs_exist():
    c = _client()
    p = _product_by_model("mp6-gas")
    csrf = _csrf(c, f"/products/{p.id}")
    r = c.post(
        f"/products/{p.id}",
        data={"model": "mp6-gas", "name": "", "category": "fan", "status": "active", "_csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "err=" in unquote(r.headers["location"])
    with Session(engine) as s:
        assert s.get(Product, p.id).category == "pump_air", "大类不应被改动"


def test_create_and_delete_product():
    c = _client()
    csrf = _csrf(c)
    r = c.post("/products", data={
        "entity_id": 2, "model": "CRT-777", "category": "pump_liquid",
        "name": "手工新建", "_csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code in (302, 303, 307), r.text
    created = _product_by_model("CRT-777")
    assert created.category == "pump_liquid"

    # 重复创建不应产生第二条
    r2 = c.post("/products", data={
        "entity_id": 2, "model": "crt-777", "category": "pump_liquid", "_csrf": csrf,
    }, follow_redirects=False)
    assert "已存在" in unquote(r2.headers["location"])

    r3 = c.post(f"/products/{created.id}/delete", data={"_csrf": csrf}, follow_redirects=False)
    assert r3.status_code == 303
    with Session(engine) as s:
        assert s.get(Product, created.id) is None


# ── 权限 ───────────────────────────────────────────────────────────────────
def test_viewer_cannot_edit_products():
    c = _client()
    csrf = _csrf(c)
    c.post("/admin/users/create", data={
        "username": "viewer_edit", "password": "ViewerPass123",
        "display_name": "只读", "role": "viewer", "_csrf": csrf,
    }, follow_redirects=False)

    v = TestClient(app)
    v.__enter__()
    v.post("/login", data={"username": "viewer_edit", "password": "ViewerPass123"},
           follow_redirects=False)
    p = _product_by_model("MZB1001T02")

    assert v.get("/products").status_code == 403
    assert v.get(f"/products/{p.id}").status_code == 403
    r = v.post(f"/products/{p.id}/delete", data={"_csrf": "x"}, follow_redirects=False)
    assert r.status_code == 403
    with Session(engine) as s:
        assert s.get(Product, p.id) is not None, "普通用户不得删除型号"


# ── 公司列表：浏览用 vs 新建用 ─────────────────────────────────────────────
def _select_block(html: str, marker: str) -> str:
    """取出某个 <select> 的完整内容，避免整页 grep 分不清是哪一处。"""
    i = html.index(marker)
    j = html.index("</select>", i)
    return html[i:j]


def test_company_filter_hides_companies_without_products():
    """0 个型号的公司在「按公司筛选」里是纯噪音 —— 选了就是个空表格。

    但「新建型号」的公司下拉必须保留它：那正是给它建第一个型号的入口。
    两处共用一份列表时，这条区分会被抹平，所以分开取 context。
    """
    from app.models import Entity

    c = _client()
    ghost = "空壳测试公司"
    with Session(engine) as s:
        exist = s.exec(select(Entity).where(Entity.name == ghost)).first()
        if exist is None:
            s.add(Entity(name=ghost, kind="competitor", tier="C"))
            s.commit()
    try:
        html = c.get("/products").text
        creatable = _select_block(html, '<select name="entity_id"')
        filt = _select_block(html, '<select id="prodCompany"')

        assert ghost in creatable, "新建型号必须能选到还没有型号的公司"
        assert ghost not in filt, "0 个型号的公司不该出现在公司筛选里"
        assert "村田制作所" in filt, "有型号的公司当然要在筛选里"
        assert html.count(ghost) == 1, f"公司名应只出现一次（新建下拉），实际 {html.count(ghost)} 次"
    finally:
        with Session(engine) as s:
            row = s.exec(select(Entity).where(Entity.name == ghost)).first()
            if row is not None:
                s.delete(row)
                s.commit()


def test_compare_picker_skips_companies_without_products():
    """对比页的型号选择区同理：company 有型号才成行。"""
    from app.models import Entity

    c = _client()
    ghost = "空壳测试公司二"
    with Session(engine) as s:
        if s.exec(select(Entity).where(Entity.name == ghost)).first() is None:
            s.add(Entity(name=ghost, kind="competitor", tier="C"))
            s.commit()
    try:
        html = c.get("/compare").text
        assert ghost not in html, "对比页不该出现没有型号的公司"
        assert "村田制作所" in html, "有型号的公司要在选择区里"
    finally:
        with Session(engine) as s:
            row = s.exec(select(Entity).where(Entity.name == ghost)).first()
            if row is not None:
                s.delete(row)
                s.commit()


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
