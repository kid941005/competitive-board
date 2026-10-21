"""表结构迁移与唯一约束的回归（F-06 + F-07）。

为什么要单独一个文件：这两个问题只在**升级**时才会咬人 ——
新库一切正常，老库升级后才发现「索引没建」或「服务起不来」。

  1. 全新库：create_all 建表，migrate 补索引并写版本号
  2. 0.7.x 老库：没有 schema_meta、没有唯一索引，还带着重复数据
     → 迁移必须先合并重复、再建索引（顺序反了会直接建失败）
  3. 库比代码新：拒绝启动，而不是等查询时 no such column
  4. 唯一索引真的在兜底：并发建同型号回 409，不是 500
  5. 同一批里出现重复项（表格两行同指标、批量接口两项同格）不能炸

两种跑法都支持：pytest tests/ 或 python tests/test_schema.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-schema-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import schema
from app.db import engine as app_engine
from app.db import init_db
from app.importer import import_rows
from app.main import app
from app.models import AuditLog, Entity, Product, ProductSpec, SpecMetric
from app.seed import seed
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel, select

_state: dict[str, object] = {}


def _admin() -> TestClient:
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


def _token() -> dict:
    if "h" not in _state:
        r = _admin().post("/api/v1/tokens",
                          json={"name": "结构测试", "scopes": ["read", "write", "admin"]})
        assert r.status_code == 201, r.text
        _state["h"] = {"Authorization": f"Bearer {r.json()['data']['token']}"}
    return _state["h"]  # type: ignore[return-value]


def _temp_engine(name: str):
    return create_engine(f"sqlite:///{Path(_TMP) / name}")


def _indexes(engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ux_%'"
        )).fetchall()
    return sorted(r[0] for r in rows)


def _version(engine) -> int:
    """还没迁移过时 schema_meta 不存在，按 0 处理。"""
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        )).scalar()
        if not exists:
            return 0
        return schema.read_version(conn)


# ── 1. 全新库 ──────────────────────────────────────────────────────────────
def test_fresh_database_gets_indexes_and_version():
    engine = _temp_engine("fresh.db")
    SQLModel.metadata.create_all(engine)
    assert _version(engine) == 0, "新库还没有版本号"
    result = schema.migrate(engine)
    assert result["to"] == schema.CURRENT_SCHEMA_VERSION
    assert _indexes(engine) == ["ux_adoptions_customer_product", "ux_products_entity_model",
                              "ux_specs_product_metric"]
    assert _version(engine) == schema.CURRENT_SCHEMA_VERSION

    # 幂等：再跑一次不重复建、不报错
    again = schema.migrate(engine)
    assert again["applied"] == []


# ── 2. 老库升级 + 重复数据合并 ─────────────────────────────────────────────
def test_upgrade_from_old_database_merges_duplicates():
    engine = _temp_engine("old.db")
    SQLModel.metadata.create_all(engine)   # 0.7.x 的建表方式：没有 schema_meta、没有唯一索引
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        s.add(Entity(id=1, kind="competitor", name="甲"))
        s.add(SpecMetric(id=1, key="flow_air", category="g", name="空气流量", unit="L/min",
                         value_type="number", better="higher", precision=2))
        s.add(SpecMetric(id=2, key="pressure_air", category="g", name="静压", unit="kPa",
                         value_type="number", better="higher", precision=2))
        s.commit()
        # 同一家公司下「MP6」与「mp6 」是同一个型号（大小写与尾部空格）
        s.add(Product(id=10, entity_id=1, model="MP6", category="pump_air"))
        s.add(Product(id=11, entity_id=1, model="mp6 ", category="pump_air"))
        s.commit()
        s.add(ProductSpec(id=100, product_id=10, metric_id=1, value_num=0.5,
                          updated_at=now - timedelta(days=1)))
        s.add(ProductSpec(id=101, product_id=10, metric_id=2, value_num=12, updated_at=now))
        s.add(ProductSpec(id=102, product_id=11, metric_id=1, value_num=0.9, updated_at=now))
        s.add(ProductSpec(id=103, product_id=11, metric_id=2, value_num=99, updated_at=now))
        s.add(ProductSpec(id=104, product_id=10, metric_id=1, value_num=0.7, updated_at=now))
        s.commit()

    schema.migrate(engine)

    with Session(engine) as s:
        products = s.exec(select(Product)).all()
        assert len(products) == 1, f"重复型号应合并成 1 条，实际 {[p.model for p in products]}"
        assert products[0].id == 10, "保留参数更多的那条"
        specs = s.exec(select(ProductSpec)).all()
        assert len(specs) == 2, f"每个指标只该留一格，实际 {len(specs)}"
        values = {sp.metric_id: sp.value_num for sp in specs}
        assert values[2] == 12, "冲突格以赢家为准"
        assert values[1] == 0.7, "同格重复保留最近更新的一条"

        logs = s.exec(select(AuditLog).where(AuditLog.origin == "migration")).all()
        assert logs, "合并动作必须留审计，否则数据凭空少了一条没人说得清"
        assert all(row.actor == "system" for row in logs)

    assert _indexes(engine) == ["ux_adoptions_customer_product", "ux_products_entity_model",
                              "ux_specs_product_metric"]


# ── 3. v3：合并风量/静压两套指标 ────────────────────────────────────────────
def test_v3_merges_fan_metrics_and_drops_vacuum():
    """跨大类对比里出现两行流量、两行静压 —— 迁移要把风扇那套并进气泵那套。

    关键不是「行数变少」，而是**量纲不能错**：风扇静压原单位是 Pa，气泵是 kPa，
    合并时必须 ÷1000，否则 1300 Pa 会变成 1300 kPa（差三个数量级，而且看起来
    只是「一个很大的值」）。
    """
    engine = _temp_engine("v3.db")
    SQLModel.metadata.create_all(engine)
    schema.migrate(engine)   # 先把索引与版本推到 3，再退回 2 模拟老库
    with Session(engine) as s:
        s.add(Entity(id=1, kind="competitor", name="甲"))
        for mid, key, name, unit, applies in (
            (1, "flow_air", "空气流量", "L/min", "pump_air"),
            (2, "pressure_air", "静压", "kPa", "pump_air"),
            (3, "vacuum_air", "真空度", "kPa", "pump_air"),
            (4, "flow_fan", "风量", "L/min", "fan"),
            (5, "pressure_fan", "静压", "Pa", "fan"),
        ):
            s.add(SpecMetric(id=mid, key=key, category="g", name=name, unit=unit,
                             value_type="number", better="higher", precision=2,
                             applies_to=applies))
        s.add(Product(id=10, entity_id=1, model="P1", category="pump_air"))
        s.add(Product(id=11, entity_id=1, model="F1", category="fan"))
        s.commit()
        s.add(ProductSpec(product_id=10, metric_id=1, value_num=0.5))
        s.add(ProductSpec(product_id=10, metric_id=3, value_num=10.5))
        s.add(ProductSpec(product_id=11, metric_id=4, value_num=1.5))
        s.add(ProductSpec(product_id=11, metric_id=5, value_num=1300))
        s.commit()
    with engine.begin() as conn:
        conn.execute(text("UPDATE schema_meta SET value = '2' WHERE key = 'version'"))

    result = schema.migrate(engine)
    assert any(step.startswith("v3") for step in result["applied"]), result

    with Session(engine) as s:
        keys = {m.key for m in s.exec(select(SpecMetric)).all()}
        assert "flow_fan" not in keys and "pressure_fan" not in keys
        assert "vacuum_air" not in keys, "真空度全库只有一条且规格书里没有，应删掉"

        press = s.exec(select(SpecMetric).where(SpecMetric.key == "pressure_air")).first()
        assert press is not None
        assert "fan" in (press.applies_to or "").split(","), "合并后静压要覆盖风扇大类"

        specs = {(sp.product_id, sp.metric_id): sp.value_num
                 for sp in s.exec(select(ProductSpec)).all()}
        assert specs[(11, press.id)] == 1.3, f"1300 Pa 应换算成 1.3 kPa，实际 {specs.get((11, press.id))}"
        assert specs[(10, 1)] == 0.5
        assert len(specs) == 3, f"真空度那条应被删除，实际剩 {len(specs)} 条"


# ── 4. 库比代码新 ──────────────────────────────────────────────────────────
def test_database_newer_than_code_refuses_to_start():
    engine = _temp_engine("future.db")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(schema._VERSION_TABLE))
        conn.execute(text("INSERT INTO schema_meta (key, value) VALUES ('version', '99')"))
    try:
        schema.migrate(engine)
    except schema.SchemaTooNew as exc:
        assert "99" in str(exc)
    else:
        raise AssertionError("库比代码新时必须拒绝启动，而不是继续跑")


# ── 4. 唯一索引与 409 ──────────────────────────────────────────────────────
def test_unique_index_is_the_last_line_of_defence():
    c = _admin()
    h = _token()
    first = c.post("/api/v1/products", headers=h,
                   json={"entity_name": "并发验证公司", "model": "RACE-1", "category": "pump_air"})
    assert first.status_code == 201, first.text

    import app.api_v1 as api

    original = api._find_product_ci
    api._find_product_ci = lambda *a, **k: None      # 模拟「查的时候还没有」
    try:
        second = c.post("/api/v1/products", headers=h,
                        json={"entity_name": "并发验证公司", "model": "RACE-1",
                              "category": "pump_air"})
    finally:
        api._find_product_ci = original

    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "UNIQUE_CONFLICT"


def test_batch_with_duplicate_items_does_not_break():
    c = _admin()
    h = _token()
    with Session(app_engine) as s:
        p = s.exec(select(Product).where(Product.model == "MZB1001T02")).first()
        assert p is not None
        pid = p.id
        mid = s.exec(select(SpecMetric).where(SpecMetric.key == "flow_air")).first().id

    r = c.post("/api/v1/specs/batch", headers=h, json={"items": [
        {"product_id": pid, "metric_key": "flow_air", "value_num": 0.11},
        {"product_id": pid, "metric_key": "flow_air", "value_num": 0.12},
    ]})
    assert r.status_code == 200, r.text
    with Session(app_engine) as s:
        rows = s.exec(select(ProductSpec).where(
            ProductSpec.product_id == pid, ProductSpec.metric_id == mid)).all()
    assert len(rows) == 1 and rows[0].value_num == 0.12


def test_import_file_with_repeated_metric_row_is_merged():
    with Session(app_engine) as s:
        rep = import_rows(
            s,
            [{"型号": "DUPD-1", "友商": "重复行测试", "大类": "压电气泵",
              "指标": "空气流量", "数值": "0.5"},
             {"型号": "DUPD-1", "友商": "重复行测试", "大类": "压电气泵",
              "指标": "空气流量", "数值": "0.6"}],
            ["型号", "友商", "大类", "指标", "数值"],
        )
        assert rep.errors == [], rep.errors
        p = s.exec(select(Product).where(Product.model == "DUPD-1")).first()
        assert p is not None
        rows = s.exec(select(ProductSpec).where(ProductSpec.product_id == p.id)).all()
    assert len(rows) == 1 and rows[0].value_num == 0.6


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
