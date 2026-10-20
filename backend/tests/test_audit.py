"""变更日志的单测。

日志的价值全在「能不能回答具体问题」，所以断言都落在细节上：
  · 改参数要能说出「从多少改到多少」，不是只记「改过了」
  · 只改条件不能被记成「值从 A 变 B」
  · 清空要记成「旧值 → 空」，删除要留快照
  · 没变化就不该产生日志（否则日志会被无意义的重提交淹没）
  · 导入要逐项记，不能只记一行「导入了 N 条」

每个用例自建数据、自清日志 —— 用例之间不共享状态，顺序无关。
两种跑法都支持：pytest tests/ 或 python tests/test_audit.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-audit-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import audit  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditLog, Product, SpecMetric  # noqa: E402
from app.seed import seed  # noqa: E402

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


def _csrf(path: str = "/products") -> str:
    html = _admin().get(path).text
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def _metric(key: str) -> SpecMetric:
    with Session(engine) as s:
        m = s.exec(select(SpecMetric).where(SpecMetric.key == key)).first()
        assert m is not None, f"指标 {key} 不存在"
        return m


def _new_product(model: str, category: str = "pump_air", entity_id: int = 2) -> int:
    r = _admin().post("/products", data={
        "entity_id": entity_id, "model": model, "category": category,
        "name": model, "_csrf": _csrf(),
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with Session(engine) as s:
        p = s.exec(select(Product).where(Product.model == model)).first()
        assert p is not None, f"型号 {model} 未创建"
        return p.id  # type: ignore[return-value]


def _set_spec(pid: int, key: str, value: str, condition: str = "") -> None:
    m = _metric(key)
    data = {f"{m.id}:v": value, f"{m.id}:c": condition, "_csrf": _csrf(f"/products/{pid}")}
    r = _admin().post(f"/products/{pid}/specs", data=data, follow_redirects=False)
    assert r.status_code == 303


def _clear_logs() -> None:
    with Session(engine) as s:
        for row in s.exec(select(AuditLog)).all():
            s.delete(row)
        s.commit()


def _logs(**filters) -> list[AuditLog]:
    with Session(engine) as s:
        rows, _total = audit.query(s, limit=500, **filters)
        return rows


def _changes(row: AuditLog) -> list[dict]:
    return audit.parse_changes(row)


def _fields(row: AuditLog) -> dict[str, dict]:
    return {c["field"]: c for c in _changes(row)}


# ── 参数改动 ───────────────────────────────────────────────────────────────
def test_spec_edit_records_old_and_new():
    pid = _new_product("AUD-1")
    _set_spec(pid, "flow_air", "0.5", "@15Vpp")
    _clear_logs()

    _set_spec(pid, "flow_air", "0.6", "@15Vpp")

    rows = _logs(action="update", object_type="spec")
    assert len(rows) == 1, f"应只有一条日志，实际 {len(rows)}"
    row = rows[0]
    assert row.object_label == "AUD-1"
    assert row.origin == "web"
    assert row.actor == "admin"
    f = _fields(row)
    assert f["空气流量"]["old"] == "0.5"
    assert f["空气流量"]["new"] == "0.6"


def test_condition_only_change_is_labelled_separately():
    """只改测试条件时，值不应表现为「从 A 变 B」。"""
    pid = _new_product("AUD-2")
    _set_spec(pid, "flow_air", "0.5", "@15Vpp")
    _clear_logs()

    _set_spec(pid, "flow_air", "0.5", "@18Vdc")

    f = _fields(_logs()[0])
    assert "空气流量" not in f, "值没变，不该出现值变更"
    assert f["空气流量 · 测试条件"]["old"] == "@15Vpp"
    assert f["空气流量 · 测试条件"]["new"] == "@18Vdc"


def test_clearing_value_is_recorded_as_emptied():
    pid = _new_product("AUD-3")
    _set_spec(pid, "flow_air", "0.4")
    _clear_logs()

    _set_spec(pid, "flow_air", "")

    f = _fields(_logs()[0])
    assert f["空气流量"]["old"] == "0.4"
    assert f["空气流量"]["new"] is None


def test_no_change_writes_no_log():
    pid = _new_product("AUD-4")
    _set_spec(pid, "flow_air", "0.3")
    _clear_logs()

    for _ in range(2):
        _set_spec(pid, "flow_air", "0.3")

    assert _logs() == [], "没变化就不该产生日志"


# ── 型号元信息与删除 ───────────────────────────────────────────────────────
def test_meta_update_records_changed_fields_only():
    pid = _new_product("AUD-5")
    _clear_logs()
    r = _admin().post(f"/products/{pid}", data={
        "model": "AUD-5", "name": "改过的名字", "category": "pump_air",
        "status": "eol", "datasheet_url": "https://x/y.pdf", "note": "",
        "_csrf": _csrf(f"/products/{pid}"),
    }, follow_redirects=False)
    assert r.status_code == 303

    f = _fields(_logs(action="update", object_type="product")[0])
    assert f["状态"]["old"] == "active" and f["状态"]["new"] == "eol"
    assert f["名称"]["new"] == "改过的名字"
    assert f["官方链接"]["new"] == "https://x/y.pdf"
    assert "型号" not in f, "型号没改，不应出现"


def test_delete_keeps_snapshot_of_lost_data():
    pid = _new_product("AUD-6", category="pump_liquid")
    _set_spec(pid, "flow_liquid", "8000", "水·100Hz")
    _clear_logs()

    r = _admin().post(f"/products/{pid}/delete", data={"_csrf": _csrf()},
                      follow_redirects=False)
    assert r.status_code == 303

    row = _logs(action="delete", object_type="product")[0]
    assert row.object_label == "AUD-6"
    f = _fields(row)
    assert "液体流量" in f, "删除前应把参数值留快照"
    assert f["液体流量"]["old"] == "8000"
    assert f["液体流量"]["new"] is None
    assert any(k.startswith("型号 · ") for k in f), "型号元信息也应在快照里"

    with Session(engine) as s:
        assert s.get(Product, pid) is None


# ── 导入 ───────────────────────────────────────────────────────────────────
def _import(csv_text: str) -> dict:
    r = _admin().post(
        "/api/import",
        files={"file": ("t.csv", csv_text.encode("utf-8"), "text/csv")},
        headers={"X-CSRF-Token": _csrf("/import")},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_import_records_each_item():
    _clear_logs()
    _import("型号,友商,大类,指标,数值\nIMP-A,导入日志甲,压电气泵,空气流量,0.9\n"
            "IMP-A,导入日志甲,压电气泵,静压,3.3\n")

    rows = _logs(action="import", object_type="spec")
    assert len(rows) == 2, f"两项参数应各记一条，实际 {len(rows)}"
    labels = {r.object_label for r in rows}
    assert "IMP-A · 空气流量" in labels
    first = next(r for r in rows if "空气流量" in r.object_label)
    change = _changes(first)[0]
    assert change["old"] is None and change["new"] == "0.9", "新写入应记为空 → 值"
    assert len({r.request_id for r in rows}) == 1, "同一批导入应共享 request_id"


def test_import_overwrite_records_old_value():
    _clear_logs()
    _import("型号,友商,大类,指标,数值\nIMP-B,导入日志乙,压电气泵,空气流量,0.9\n")
    _clear_logs()
    _import("型号,友商,大类,指标,数值\nIMP-B,导入日志乙,压电气泵,空气流量,0.95\n")

    change = _changes(_logs(action="import")[0])[0]
    assert change["old"] == "0.9" and change["new"] == "0.95"


# ── 查询、权限与清理 ───────────────────────────────────────────────────────
def test_query_filters_and_keyword():
    pid = _new_product("AUD-7")
    _set_spec(pid, "flow_air", "1.1")
    _clear_logs()
    _set_spec(pid, "flow_air", "1.2")

    rows = _logs(q="AUD-7")
    assert rows and all("AUD-7" in r.object_label for r in rows)
    assert _logs(actor="admin")
    assert _logs(actor="不存在的人") == []
    assert _logs(action="delete") == []
    assert _logs(object_type="spec")


def test_logs_page_renders_diff():
    pid = _new_product("AUD-8")
    _set_spec(pid, "flow_air", "2.0")
    _clear_logs()
    _set_spec(pid, "flow_air", "2.1")

    r = _admin().get("/admin/logs?days=1")
    assert r.status_code == 200
    assert "变更内容" in r.text
    assert "→" in r.text
    assert "AUD-8" in r.text


def test_viewer_cannot_see_logs():
    admin = _admin()
    csrf = _csrf("/admin/users")
    admin.post("/admin/users/create", data={
        "username": "viewer_log", "password": "ViewerPass123",
        "display_name": "只读", "role": "viewer", "_csrf": csrf,
    }, follow_redirects=False)

    v = TestClient(app)
    v.__enter__()
    v.post("/login", data={"username": "viewer_log", "password": "ViewerPass123"},
           follow_redirects=False)
    assert v.get("/admin/logs").status_code == 403


def test_api_audit_requires_admin_scope():
    admin = _admin()
    _new_product("AUD-9")  # 保证至少有一条日志

    read_token = admin.post("/api/v1/tokens",
                            json={"name": "只读看日志", "scopes": ["read"]}).json()["data"]["token"]
    r = admin.get("/api/v1/audit", headers={"Authorization": f"Bearer {read_token}"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"

    admin_token = admin.post("/api/v1/tokens",
                             json={"name": "管理员看日志", "scopes": ["admin"]}).json()["data"]["token"]
    r = admin.get("/api/v1/audit?limit=5&object_type=product",
                  headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["data"], "应能读到日志"
    assert "changes" in body["data"][0]
    assert body["meta"]["total"] >= 1


def test_purge_removes_only_expired():
    _admin()
    with Session(engine) as s:
        s.add(AuditLog(
            at=datetime.now(timezone.utc) - timedelta(days=400),
            actor="远古", action="update", object_type="spec",
            object_label="OLD-1", changes='[{"field":"x","old":"1","new":"2"}]',
        ))
        s.add(AuditLog(
            at=datetime.now(timezone.utc), actor="今天", action="update",
            object_type="spec", object_label="NEW-1", changes="[]",
        ))
        s.commit()

    with Session(engine) as s:
        removed = audit.purge(s, 180)
        assert removed == 1, f"应只清理 1 条过期日志，实际 {removed}"
        assert not s.exec(select(AuditLog).where(AuditLog.actor == "远古")).first()
        assert s.exec(select(AuditLog).where(AuditLog.actor == "今天")).first(), "未过期的不许删"


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
