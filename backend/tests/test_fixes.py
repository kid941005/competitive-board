"""代码审查（2026-10-20）修复的回归测试。

每条对应报告里一个编号，断言的是「原来的错误行为不再发生」，而不是实现细节：
  F-01 导入跨公司串号        F-02 dry_run 毒化幂等键
  F-03 置信度 0 变 1.0       F-04 机器导入后仍算已复核
  F-05 内联 onsubmit 注入    F-08 导入无体积上限
  F-11 账号操作无审计        P2 逗号小数解析

两种跑法都支持：pytest tests/ 或 python tests/test_fixes.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-fix-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import settings_store, tokens
from app.audit import AuditLog
from app.db import engine, init_db
from app.importer import import_rows
from app.main import app
from app.models import Product, ProductSpec, SpecMetric
from app.numeric import parse_number
from app.seed import seed
from fastapi.testclient import TestClient
from sqlmodel import Session, select

ADMIN_PW = "AdminPass123"
_state: dict[str, object] = {}


def _c() -> TestClient:
    if "c" in _state:
        return _state["c"]  # type: ignore[return-value]
    init_db()
    seed()
    client = TestClient(app)
    client.__enter__()
    client.post("/login", data={"username": "admin", "password": ADMIN_PW}, follow_redirects=False)
    _state["c"] = client
    return client


def _csrf(client: TestClient, path: str = "/products") -> str:
    html = client.get(path).text
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def _metric_id(session: Session, key: str) -> int:
    metric = session.exec(select(SpecMetric).where(SpecMetric.key == key)).first()
    assert metric is not None and metric.id is not None
    return metric.id


# ── F-01 跨公司串号 ────────────────────────────────────────────────────────
def test_import_same_model_two_companies():
    _c()
    rows = [
        {"型号": "SHARED-1", "友商": "甲测试公司", "大类": "压电气泵", "指标": "空气流量", "数值": "0.5"},
        {"型号": "SHARED-1", "友商": "乙测试公司", "大类": "压电气泵", "指标": "空气流量", "数值": "0.9"},
    ]
    with Session(engine) as s:
        import_rows(s, rows, ["型号", "友商", "大类", "指标", "数值"])
        found = s.exec(select(Product).where(Product.model == "SHARED-1")).all()
        assert len(found) == 2, f"两家公司同型号应建 2 条，实际 {len(found)} 条"

        mid = _metric_id(s, "flow_air")
        values = {
            p.entity_id: s.exec(
                select(ProductSpec).where(
                    ProductSpec.product_id == p.id, ProductSpec.metric_id == mid
                )
            ).first().value_num
            for p in found
        }
        assert sorted(values.values()) == [0.5, 0.9], f"两家的值不应互相覆盖：{values}"


# ── F-03 置信度 ────────────────────────────────────────────────────────────
def test_import_confidence_zero_and_clamp():
    _c()
    rows = [
        {"型号": "CONF-1", "友商": "置信测试", "大类": "压电气泵", "指标": "静压", "数值": "1.2", "置信度": "0"},
        {"型号": "CONF-1", "友商": "置信测试", "大类": "压电气泵", "指标": "空气流量", "数值": "0.8", "置信度": "5"},
    ]
    with Session(engine) as s:
        import_rows(s, rows, ["型号", "友商", "大类", "指标", "数值", "置信度"])
        product = s.exec(select(Product).where(Product.model == "CONF-1")).first()
        press = s.exec(
            select(ProductSpec).where(
                ProductSpec.product_id == product.id, ProductSpec.metric_id == _metric_id(s, "pressure_air")
            )
        ).first()
        flow = s.exec(
            select(ProductSpec).where(
                ProductSpec.product_id == product.id, ProductSpec.metric_id == _metric_id(s, "flow_air")
            )
        ).first()
    assert press.confidence == 0.0, f"置信度 0 应原样落库，实际 {press.confidence}"
    assert flow.confidence == 1.0, f"置信度 5 应被钳到 1.0，实际 {flow.confidence}"


# ── F-04 覆盖后应回到未复核 ────────────────────────────────────────────────
def test_import_resets_verified():
    _c()
    rows = [{"型号": "VER-1", "友商": "复核测试", "大类": "压电气泵", "指标": "空气流量", "数值": "0.7"}]
    headers = ["型号", "友商", "大类", "指标", "数值"]
    with Session(engine) as s:
        import_rows(s, rows, headers)
        product = s.exec(select(Product).where(Product.model == "VER-1")).first()
        mid = _metric_id(s, "flow_air")
        spec = s.exec(
            select(ProductSpec).where(ProductSpec.product_id == product.id, ProductSpec.metric_id == mid)
        ).first()
        spec.verified = True
        s.add(spec)
        s.commit()

        rows[0]["数值"] = "0.72"
        import_rows(s, rows, headers)
        s.refresh(spec)
    assert spec.value_num == 0.72
    assert spec.verified is False, "机器重新写入后，人工复核标记必须失效"


# ── F-02 dry_run 不得占用幂等键 ────────────────────────────────────────────
def test_dry_run_does_not_poison_idempotency():
    _c()
    with Session(engine) as s:
        raw, _ = tokens.create_token(s, name="幂等测试", scopes=["read", "write"])
    client = _c()
    headers = {"Authorization": f"Bearer {raw}", "Idempotency-Key": "FIX-KEY-1"}
    body = {"entity_name": "幂等测试公司", "model": "IDEM-FIX", "category": "pump_air"}

    preview = client.post("/api/v1/products", json={**body, "dry_run": True}, headers=headers)
    assert preview.status_code == 200
    assert preview.json()["meta"].get("dry_run") is True

    real = client.post("/api/v1/products", json=body, headers=headers)
    assert real.status_code == 201, "预演之后的真实调用不应被当成重放"
    assert not real.json()["meta"].get("replayed")
    with Session(engine) as s:
        assert s.exec(select(Product).where(Product.model == "IDEM-FIX")).first() is not None


# ── F-05 页面不再有内联事件处理器 ──────────────────────────────────────────
def test_no_inline_event_handlers():
    _c()
    with Session(engine) as s:
        s.add(Product(entity_id=2, model="X');alert(1);//", name="注入", category="pump_air"))
        s.commit()
    client = _c()
    for path in ("/products", "/admin/tokens", "/admin/logs"):
        html = client.get(path).text
        assert "onsubmit=" not in html, f"{path} 仍有内联 onsubmit"
        assert "data-confirm=" in html or path == "/admin/users"
    assert "/static/confirm.js" in client.get("/products").text


# ── F-08 导入体积上限 ──────────────────────────────────────────────────────
def test_import_respects_size_limit():
    client = _c()
    csrf = _csrf(client, "/import")
    with Session(engine) as s:
        original = settings_store.get_value(s, "datasheet.max_upload_mb")
        settings_store.update(s, "datasheet.max_upload_mb", 1, "test")
    try:
        big = ("型号,指标,数值\n" + "BIG-1,空气流量,0.5\n" * 120_000).encode("utf-8")
        assert len(big) > 1024 * 1024
        r = client.post(
            "/api/import",
            files={"file": ("big.csv", big, "text/csv")},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 413, f"超限应 413，实际 {r.status_code}"
    finally:
        with Session(engine) as s:
            settings_store.update(s, "datasheet.max_upload_mb", original, "test")


# ── F-11 账号操作要留痕 ────────────────────────────────────────────────────
def test_user_password_reset_is_audited():
    client = _c()
    csrf = _csrf(client, "/admin/users")
    created = client.post(
        "/admin/users/create",
        data={
            "username": "audit_target",
            "password": "ViewerPass123",
            "display_name": "审计对象",
            "role": "viewer",
            "_csrf": csrf,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    with Session(engine) as s:
        from app.models import User

        uid = s.exec(select(User).where(User.username == "audit_target")).first().id

    client.post(
        f"/admin/users/{uid}/password",
        data={"password": "NewPass12345", "_csrf": _csrf(client, "/admin/users")},
        follow_redirects=False,
    )

    with Session(engine) as s:
        rows = s.exec(
            select(AuditLog).where(
                AuditLog.object_type == "user", AuditLog.object_id == uid
            )
        ).all()
    assert rows, "账号操作没有产生任何审计日志"
    assert any("重置密码" in (r.note or "") for r in rows), [r.note for r in rows]


# ── 数值解析：逗号两种含义 ─────────────────────────────────────────────────
def test_parse_number_comma_variants():
    assert parse_number("1,5") == 1.5
    assert parse_number("1,234") == 1234.0
    assert parse_number("1,234.56") == 1234.56
    assert parse_number("0.7") == 0.7
    assert parse_number("") is None
    assert parse_number("abc") is None
    assert parse_number(0) == 0.0
    assert parse_number(False) is None or parse_number(False) == 0.0


# ── 状态只允许三种 ─────────────────────────────────────────────────────────
def test_invalid_status_rejected():
    client = _c()
    r = client.post(
        "/products",
        data={
            "entity_id": 2,
            "model": "STATUS-1",
            "category": "pump_air",
            "status": "whatever",
            "_csrf": _csrf(client, "/products"),
        },
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 400, f"非法状态应 400，实际 {r.status_code}"
    with Session(engine) as s:
        assert s.exec(select(Product).where(Product.model == "STATUS-1")).first() is None


# ── 批量写参数：预加载重构后行为不变 ───────────────────────────────────────
def test_batch_specs_still_correct():
    _c()
    with Session(engine) as s:
        raw, _ = tokens.create_token(s, name="批量回归", scopes=["read", "write"])
        pid = s.exec(select(Product).where(Product.model == "MZB1001T02")).first().id
    headers = {"Authorization": f"Bearer {raw}"}
    body = {"items": [
        {"product_id": pid, "metric_key": "flow_air", "value_num": 0.71, "verified": True},
        {"product_id": pid, "metric_key": "pressure_air", "value_num": 1.5},
        {"product_id": pid, "metric_key": "flow_liquid", "value_num": 9},   # 大类不符
    ]}
    r = _c().post("/api/v1/specs/batch", json=body, headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["applied"] == 2 and r.json()["meta"]["failed"] == 1

    with Session(engine) as s:
        mid = _metric_id(s, "flow_air")
        spec = s.exec(
            select(ProductSpec).where(ProductSpec.product_id == pid, ProductSpec.metric_id == mid)
        ).first()
    assert spec is not None and spec.value_num == 0.71
    assert spec.verified is True


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
