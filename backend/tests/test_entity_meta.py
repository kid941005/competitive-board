"""公司元信息：`POST /entities` 的字段透传 + `PATCH /entities/{id}`。

回归的是一个真实踩到的 bug：`EntityIn` 声明了 `kind` / `name_en` / `region` / `tier`，
但 `_resolve_entity_for_create` 建实体时硬编码 `kind="competitor", tier="C"`，
四个字段全部被丢弃。表现是：调用方传 `kind=self` 建出来的仍是 competitor，
而 API 又没有改实体的接口 —— 错数据只能靠改库。

    python tests/test_entity_meta.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-ent-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Entity, Product
from app.seed import seed
from fastapi.testclient import TestClient
from sqlmodel import Session, select

ADMIN_PW = "AdminPass123"
_state: dict[str, object] = {}


def _boot() -> TestClient:
    if _state.get("c"):
        return _state["c"]  # type: ignore[return-value]
    init_db()
    client = TestClient(app)
    client.__enter__()
    r = client.post("/login", data={"username": "admin", "password": ADMIN_PW},
                    follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    _state["c"] = client
    return client


def _entities() -> dict[str, dict]:
    with Session(engine) as s:
        return {
            e.name: {"id": e.id, "kind": e.kind, "name_en": e.name_en,
                     "region": e.region, "tier": e.tier}
            for e in s.exec(select(Entity)).all()
        }


def _audits(entity_id: int) -> list[AuditLog]:
    with Session(engine) as s:
        return list(s.exec(
            select(AuditLog)
            .where(AuditLog.object_type == "entity", AuditLog.object_id == entity_id)
            .order_by(AuditLog.id)  # type: ignore[attr-defined]
        ).all())


# ── 导入时的公司类型 ───────────────────────────────────────────────────────
def test_import_creates_self_company_by_name():
    """导入表的列名叫「友商」，但自家参数也会走同一条路 —— 按名字判成 self。

    回归：`import_rows` 曾经一律 `Entity(kind="competitor")`，于是导入自家产品时
    左蓝微被建成竞争对手，对比页把它归到友商那一栏。
    """
    c = _boot()
    seed(metrics_only=True)      # 只建指标字典，不建任何公司

    r = c.post("/api/v1/import", json={"rows": [
        # 宽表：一行一个型号，指标是列
        {"企业名称": config.SELF_COMPANY_NAME, "产品型号": "OWN-1",
         "产品类别": "压电气泵", "流量L/min": "0.5"},
        {"企业名称": "导入友商", "产品型号": "RIVAL-1",
         "产品类别": "压电气泵", "流量L/min": "0.6"},
    ]})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["data"]["created_entities"] == 2, r.json()["data"]

    ents = _entities()
    assert ents[config.SELF_COMPANY_NAME]["kind"] == "self", "自家公司被建成了友商"
    assert ents["导入友商"]["kind"] == "competitor"


# ── 建实体：字段必须真的生效 ───────────────────────────────────────────────
def test_create_entity_keeps_all_fields():
    """回归点：以前 kind/name_en/region/tier 会被静默丢弃。

    不用「左蓝微」——上一条用例已经建过它，命中同名会走「复用」分支，
    测不到新建时的字段透传。
    """
    c = _boot()
    r = c.post("/api/v1/entities", json={
        "name": "字段透传公司", "kind": "self", "name_en": "FIELD-TEST",
        "region": "中国", "tier": "A",
    })
    assert r.status_code in (200, 201), r.text[:300]
    data = r.json()["data"]
    assert data["kind"] == "self", f"kind 被丢弃了，实际 {data['kind']}"
    assert data["name_en"] == "FIELD-TEST"
    assert data["region"] == "中国"
    assert data["tier"] == "A"

    # 再查一遍，确认落库而不只是响应里好看
    got = _entities()["字段透传公司"]
    assert (got["kind"], got["name_en"], got["region"], got["tier"]) == (
        "self", "FIELD-TEST", "中国", "A")


def test_create_entity_defaults_stay_competitor():
    """不带 kind 时仍是友商 —— 建型号时「顺便建公司」依赖这个默认。"""
    c = _boot()
    r = c.post("/api/v1/entities", json={"name": "某友商"})
    assert r.status_code in (200, 201)
    assert r.json()["data"]["kind"] == "competitor"
    assert r.json()["data"]["tier"] == "C"


def test_create_entity_rejects_bad_kind():
    c = _boot()
    r = c.post("/api/v1/entities", json={"name": "怪公司", "kind": "partner"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert "怪公司" not in _entities()


def test_create_entity_rejects_bad_tier():
    c = _boot()
    r = c.post("/api/v1/entities", json={"name": "怪档位", "tier": "S"})
    assert r.status_code == 422
    assert "怪档位" not in _entities()


# ── 改实体 ─────────────────────────────────────────────────────────────────
def test_patch_entity_fixes_kind():
    """这条正是回填时遇到的：先被建成 competitor，事后要改成 self。"""
    c = _boot()
    eid = _entities()["某友商"]["id"]
    r = c.patch(f"/api/v1/entities/{eid}", json={"kind": "self", "tier": "B"})
    assert r.status_code == 200, r.text[:300]
    got = _entities()["某友商"]
    assert got["kind"] == "self" and got["tier"] == "B"


def test_patch_entity_updates_meta_and_audits():
    c = _boot()
    ent = _entities()["某友商"]
    before = len(_audits(ent["id"]))
    r = c.patch(f"/api/v1/entities/{ent['id']}",
                json={"name": "改名后的公司", "name_en": "Renamed Co", "region": "德国"})
    assert r.status_code == 200
    got = _entities()["改名后的公司"]
    assert (got["name_en"], got["region"]) == ("Renamed Co", "德国")

    rows = _audits(ent["id"])
    assert len(rows) == before + 1, "改元信息必须留审计"
    last = rows[-1]
    assert last.action == "update"
    assert "名称" in last.changes and "英文名" in last.changes, last.changes
    assert last.object_label == "某友商", "label 应是旧名，便于按旧名检索"


def test_patch_entity_duplicate_name_rejected():
    c = _boot()
    ent = _entities()["改名后的公司"]
    r = c.patch(f"/api/v1/entities/{ent['id']}", json={"name": "左蓝微"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ENTITY_EXISTS"
    assert "改名后的公司" in _entities(), "被拒时不能改成功"


def test_patch_entity_cannot_become_customer_with_products():
    """客户不是竞品来源 —— 名下还有型号时改成 customer 会让型号失去归属。"""
    c = _boot()
    r = c.post("/api/v1/products", json={
        "entity_name": "有型号的公司", "model": "P-1", "category": "pump_air"})
    assert r.status_code in (200, 201), r.text[:300]
    eid = _entities()["有型号的公司"]["id"]

    r = c.patch(f"/api/v1/entities/{eid}", json={"kind": "customer"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert "型号" in r.json()["error"]["message"]
    assert _entities()["有型号的公司"]["kind"] == "competitor"

    with Session(engine) as s:
        assert s.exec(select(Product).where(Product.entity_id == eid)).first() is not None


def test_patch_entity_dry_run_does_not_persist():
    c = _boot()
    ent = _entities()["有型号的公司"]
    r = c.patch(f"/api/v1/entities/{ent['id']}", json={"name": "预演改名", "dry_run": True})
    assert r.status_code == 200
    assert r.json()["meta"]["dry_run"] is True
    assert "有型号的公司" in _entities(), "预演不能落库"
    assert "预演改名" not in _entities()


def test_patch_entity_no_change_is_quiet():
    c = _boot()
    ent = _entities()["有型号的公司"]
    before = len(_audits(ent["id"]))
    r = c.patch(f"/api/v1/entities/{ent['id']}", json={"tier": "C"})
    assert r.status_code == 200
    assert len(_audits(ent["id"])) == before, "没变化不该写日志"


def test_patch_entity_not_found():
    c = _boot()
    r = c.patch("/api/v1/entities/99999", json={"kind": "self"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_manifest_lists_update_entity():
    c = _boot()
    tools = {t["name"]: t for t in c.get("/api/v1/tools").json()["data"]["tools"]}
    assert "update_entity" in tools
    # /tools 用的是 parameters（MCP 层才把它转成 inputSchema）
    spec = tools["update_entity"]
    props = spec["parameters"]["properties"]
    assert set(props) >= {"entity_id", "name", "kind", "name_en", "region", "tier"}
    assert "entity_id" in spec["parameters"]["required"]


ORDER = [
    test_import_creates_self_company_by_name,
    test_create_entity_keeps_all_fields,
    test_create_entity_defaults_stay_competitor,
    test_create_entity_rejects_bad_kind,
    test_create_entity_rejects_bad_tier,
    test_patch_entity_fixes_kind,
    test_patch_entity_updates_meta_and_audits,
    test_patch_entity_duplicate_name_rejected,
    test_patch_entity_cannot_become_customer_with_products,
    test_patch_entity_dry_run_does_not_persist,
    test_patch_entity_no_change_is_quiet,
    test_patch_entity_not_found,
    test_manifest_lists_update_entity,
]


def main() -> int:
    failed = 0
    for fn in ORDER:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}  {exc}")
        except Exception as exc:
            failed += 1
            print(f"  ERROR {fn.__name__}  {type(exc).__name__}: {exc}")
    print(f"\n{len(ORDER) - failed}/{len(ORDER)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
