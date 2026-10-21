"""规格书导入的单测：建档（新公司 / 新型号）与三类拦截。

导入在本项目里的定位是「扩充数据库」，所以重点不是解析准确率（那是 test_datasheet.py），
而是**建档是否正确**：
  · 型号不存在 → 新建型号；存在 → 更新，且不重复建
  · 公司写法不同（村田 / 村田制作所）→ 拦住，不建两份档案
  · 同一型号不能跨大类

两种跑法都支持：
    pytest tests/
    python tests/test_datasheet_import.py

使用独立临时数据目录，不碰开发库。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-import-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine, init_db
from app.main import app
from app.models import Entity, Product, ProductSpec
from app.seed import seed
from fastapi.testclient import TestClient
from sqlmodel import Session, select

DOC = "空气流量 0.7 L/min\n静压 1.42 kPa\n重量 1.4 g\n"

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


def _csrf(c: TestClient) -> str:
    html = c.get("/datasheet").text
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def _analyze(c: TestClient, *, entity_id=None, entity_name=None, category="pump_air",
             model="T-100", text=DOC):
    data = {"category": category, "model": model}
    if entity_id is not None:
        data["entity_id"] = str(entity_id)
    if entity_name is not None:
        data["entity_name"] = entity_name
    r = c.post(
        "/api/datasheet/analyze",
        data=data,
        files={"file": ("spec.txt", text.encode("utf-8"), "text/plain")},
        headers={"X-CSRF-Token": _csrf(c)},
    )
    return r


def _commit(c: TestClient, doc_id: str, target: dict, rows=None):
    payload = {
        "doc_id": doc_id,
        "target": target,
        "rows": rows
        if rows is not None
        else [
            {"metric_key": "flow_air", "value_num": 0.7, "confidence": 0.9},
            {"metric_key": "pressure_air", "value_num": 1.42, "confidence": 0.9},
        ],
    }
    return c.post("/api/datasheet/commit", json=payload,
                  headers={"X-CSRF-Token": _csrf(c)})


# ── 分析阶段 ───────────────────────────────────────────────────────────────
def test_analyze_returns_target_and_metric_pool():
    c = _client()
    r = _analyze(c, entity_id=2, model="MZB1001T02")  # 村田现有型号
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["target"]["product_is_new"] is False, "已有型号应被识别为更新"
    keys = {m["key"] for m in d["metrics"]}
    assert "flow_air" in keys and "flow_liquid" not in keys, "指标池必须按大类裁剪"
    assert "flow_air" in {x["metric_key"] for x in d["candidates"]}


def test_analyze_rejects_unknown_category_and_empty_model():
    c = _client()
    assert _analyze(c, entity_id=2, category="ceramic").status_code == 400
    assert _analyze(c, entity_id=2, model="   ").status_code == 400


def test_analyze_rejects_customer_entity():
    c = _client()
    with Session(engine) as s:
        cust = Entity(kind="customer", name="某客户", tier="C")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
    r = _analyze(c, entity_id=cid, model="X-1")
    assert r.status_code == 400, r.text


# ── 建档 ───────────────────────────────────────────────────────────────────
def test_new_company_and_new_model_are_created():
    """导入的核心用途：新友商 + 新型号一次建档并落参数。"""
    c = _client()
    r = _analyze(c, entity_name="建档测试甲", model="NX-1001")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["target"]["entity_is_new"] is True
    assert d["target"]["product_is_new"] is True

    r2 = _commit(c, d["doc_id"], {
        "entity_name": "建档测试甲", "category": "pump_air", "model": "NX-1001",
    })
    assert r2.status_code == 200, r2.text
    res = r2.json()
    assert res["entity_created"] is True, res
    assert res["product_created"] is True, res
    assert res["written"] == 2, res

    with Session(engine) as s:
        ent = s.exec(select(Entity).where(Entity.name == "建档测试甲")).first()
        assert ent is not None
        prod = s.exec(select(Product).where(Product.model == "NX-1001")).first()
        assert prod is not None and prod.entity_id == ent.id
        assert prod.category == "pump_air"
        specs = s.exec(select(ProductSpec).where(ProductSpec.product_id == prod.id)).all()
        assert len(specs) == 2
        assert all(x.verified is False for x in specs), "导入的参数必须标记未复核"


def test_existing_model_is_updated_not_duplicated():
    c = _client()
    name, model = "建档测试乙", "NX-2002"

    # 先建一次，确保后续走的是「更新」路径
    first = _analyze(c, entity_name=name, model=model)
    d0 = first.json()
    assert d0["target"]["product_is_new"] is True
    assert _commit(c, d0["doc_id"], {
        "entity_name": name, "category": "pump_air", "model": model,
    }).json()["product_created"] is True

    # 型号大小写不同，仍应视为同一款
    r = _analyze(c, entity_name=name, model="nx-2002")
    d = r.json()
    assert d["target"]["product_is_new"] is False, "同型号（忽略大小写）应复用"

    r2 = _commit(c, d["doc_id"], {
        "entity_name": name, "category": "pump_air", "model": "nx-2002",
    })
    res = r2.json()
    assert res["product_created"] is False
    assert res["updated"] == 2 and res["written"] == 0, res

    with Session(engine) as s:
        n = len(s.exec(select(Product).where(Product.model.ilike("nx-2002"))).all())  # type: ignore[attr-defined]
    assert n == 1, f"型号被重复建档了：{n} 条"


def test_similar_company_name_is_blocked():
    """「村田」与已有的「村田制作所」必须拦住 —— 否则同一家会有两份档案。"""
    c = _client()
    r = _analyze(c, entity_name="村田", model="NEW-1")
    assert r.status_code == 200
    r2 = _commit(c, r.json()["doc_id"], {
        "entity_name": "村田", "category": "pump_air", "model": "NEW-1",
    })
    assert r2.status_code == 409, r2.text
    assert "相似公司" in r2.json()["detail"]

    with Session(engine) as s:
        assert s.exec(select(Entity).where(Entity.name == "村田")).first() is None, "不应建出重复公司"


def test_force_new_entity_allows_explicit_duplicate():
    """确认是另一家时可以显式放行 —— 拦截是提醒，不是禁令。"""
    c = _client()
    r = _analyze(c, entity_name="巴特尔斯", model="NEW-2")
    r2 = _commit(c, r.json()["doc_id"], {
        "entity_name": "巴特尔斯", "category": "pump_air", "model": "NEW-2",
        "force_new_entity": True,
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["entity_created"] is True


def test_model_category_conflict_is_rejected():
    """同一型号不能跨大类：mp6-liq 是液泵，按气泵导入必须拒绝。"""
    c = _client()
    with Session(engine) as s:
        bartels = s.exec(select(Entity).where(Entity.name.like("%巴特尔斯%"))).first()  # type: ignore[attr-defined]
        assert bartels is not None
        bid = bartels.id

    r = _analyze(c, entity_id=bid, category="pump_air", model="mp6-liq")
    assert r.status_code == 200
    assert r.json()["warning"], "分析阶段就应预警大类冲突"

    r2 = _commit(c, r.json()["doc_id"], {
        "entity_id": bid, "category": "pump_air", "model": "mp6-liq",
    })
    assert r2.status_code == 409, r2.text
    assert "不一致" in r2.json()["detail"]


def test_commit_requires_rows():
    c = _client()
    r = _analyze(c, entity_name="建档测试甲", model="NX-1001")
    r2 = _commit(c, r.json()["doc_id"], {
        "entity_name": "建档测试甲", "category": "pump_air", "model": "NX-1001",
    }, rows=[])
    assert r2.status_code == 400


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
