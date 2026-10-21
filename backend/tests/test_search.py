"""检索接口的回归（A+B 里的 B 部分）。

守四件事：
  1. `q` 认公司名、产品名、型号 —— 原来只认型号字面量，搜「村田」一条都出不来
  2. 多词按 AND：`村田 MZB` 要能命中，而不是要求连在一起
  3. `%` / `_` 按字面量处理（原来 `q=%` 会返回全库）
  4. `matched` 说清命中了哪个字段，`meta.q` 回显实际使用的关键词

两种跑法都支持：pytest tests/ 或 python tests/test_search.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-search-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import search
from app.db import init_db
from app.main import app
from app.seed import seed
from fastapi.testclient import TestClient

_state: dict[str, object] = {}


def _h() -> dict:
    if "h" in _state:
        return _state["h"]  # type: ignore[return-value]
    init_db()
    seed()
    c = TestClient(app)
    c.__enter__()
    c.post("/login", data={"username": "admin", "password": "AdminPass123"},
           follow_redirects=False)
    r = c.post("/api/v1/tokens", json={"name": "检索测试", "scopes": ["read"]})
    assert r.status_code == 201, r.text
    _state["c"] = c
    _state["h"] = {"Authorization": f"Bearer {r.json()['data']['token']}"}
    return _state["h"]  # type: ignore[return-value]


def _get(path: str) -> dict:
    headers = _h()                      # 首次调用会把 client 与 token 建好
    c: TestClient = _state["c"]  # type: ignore[assignment]
    r = c.get(path, headers=headers)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"
    return r.json()


# ── 纯函数：转义与多词 ─────────────────────────────────────────────────────
def test_escapes_like_wildcards():
    assert search.escape_like("100%") == "100\\%"
    assert search.escape_like("a_b") == "a\\_b"
    # 反斜杠先转义，否则会把自己加的转义符再转一遍
    assert search.escape_like("a\\b") == "a\\\\b"
    assert search.pattern("50%") == "%50\\%%"


def test_normalize_trims_and_caps():
    assert search.normalize("  村田  ") == "村田"
    assert search.normalize("   ") is None
    assert search.normalize(None) is None
    long = "x" * 200
    assert len(search.normalize(long) or "") == search.MAX_QUERY_LEN


# ── 型号检索 ───────────────────────────────────────────────────────────────
def test_products_search_by_company_name():
    body = _get("/api/v1/products?q=村田")
    assert body["meta"]["q"] == "村田"
    rows = body["data"]
    assert len(rows) == 4, f"村田有 4 个型号，实际 {len(rows)}"
    for row in rows:
        assert row["entity"]["name"] == "村田制作所"
        assert "company" in row["matched"], row["matched"]


def test_products_search_by_model_and_by_product_name():
    by_model = _get("/api/v1/products?q=MZB1001")
    assert [r["model"] for r in by_model["data"]] == ["MZB1001T02"]
    assert "model" in by_model["data"][0]["matched"]

    by_name = _get("/api/v1/products?q=微泵")
    assert len(by_name["data"]) == 5, by_name["data"]
    assert all("name" in r["matched"] for r in by_name["data"])


def test_products_multi_word_is_and_across_fields():
    body = _get("/api/v1/products?q=村田%20MZB")
    assert len(body["data"]) == 4, body["data"]
    # 公司名只命中前半个词、型号只命中后半个词，两个字段都该出现在解释里
    assert set(body["data"][0]["matched"]) >= {"company", "model"}

    none = _get("/api/v1/products?q=村田%20zzz")
    assert none["data"] == [], "只命中一个词不该算匹配"


def test_products_wildcards_are_literal():
    assert _get("/api/v1/products?q=%25")["data"] == [], "% 应按字面量处理，而不是匹配全部"
    assert _get("/api/v1/products?q=_")["data"] == [], "_ 应按字面量处理"


def test_products_empty_query_means_no_filter():
    body = _get("/api/v1/products?q=")
    assert len(body["data"]) == 11, len(body["data"])
    assert "q" not in body["meta"], "没关键词时不该回显 q"
    assert all("matched" not in r for r in body["data"]), "没关键词时不该带 matched"


def test_products_search_combines_with_category():
    both = _get("/api/v1/products?q=村田&category=pump_air")
    assert len(both["data"]) == 4, both["data"]
    none = _get("/api/v1/products?q=村田&category=fan")
    assert none["data"] == [], "村田没有风扇型号，组合筛选应为空"


# ── 公司检索 ───────────────────────────────────────────────────────────────
def test_entities_search_by_chinese_and_english_name():
    zh = _get("/api/v1/entities?q=村田")
    assert [e["name"] for e in zh["data"]] == ["村田制作所"]
    assert zh["data"][0]["matched"] == ["name"]

    en = _get("/api/v1/entities?q=murata")
    assert [e["name"] for e in en["data"]] == ["村田制作所"]
    assert "name_en" in en["data"][0]["matched"]

    assert _get("/api/v1/entities?q=zzz")["data"] == []


def test_entities_search_combines_with_kind():
    body = _get("/api/v1/entities?q=村田&kind=self")
    assert body["data"] == [], "左蓝微不叫村田，按 kind 过滤后应为空"


# ── 指标检索 ───────────────────────────────────────────────────────────────
def test_metrics_search_by_name_key_and_alias():
    # 「静压」现在只有一项：风扇那套 pressure_fan 已经在 v3 迁移里并进 pressure_air，
    # 否则跨大类对比会出现两个同名指标。
    by_name = _get("/api/v1/metrics?q=静压")
    keys = sorted(m["key"] for m in by_name["data"])
    assert keys == ["pressure_air"], keys

    by_key = _get("/api/v1/metrics?q=flow")
    assert len(by_key["data"]) == 2, by_key["data"]  # flow_air + flow_liquid
    assert all("key" in m["matched"] for m in by_key["data"])

    # 别名：airflow 在 flow_air 的 aliases 里（合并后「风量」也归到这一项）
    by_alias = _get("/api/v1/metrics?q=airflow")
    assert [m["key"] for m in by_alias["data"]] == ["flow_air"], by_alias["data"]


def test_metrics_search_combines_with_category():
    body = _get("/api/v1/metrics?q=pressure&category=fan")
    assert [m["key"] for m in body["data"]] == ["pressure_air"]
    assert _get("/api/v1/metrics?q=zzz")["data"] == []


# ── 页面入口 ───────────────────────────────────────────────────────────────
def test_pages_expose_search_inputs():
    _h()
    c: TestClient = _state["c"]  # type: ignore[assignment]
    compare = c.get("/compare").text
    assert 'id="chipSearch"' in compare, "对比页缺少型号搜索框"
    assert 'data-haystack=' in compare, "型号 chip 缺少待匹配文本"

    products = c.get("/products").text
    for frag in ('id="prodSearch"', 'id="prodCompany"', 'id="prodCat"'):
        assert frag in products, f"产品管理页缺少 {frag}"


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
