"""通道一致性测试 —— 第二轮审查（2026-10-20）的回归。

这个文件守的不是某一条路由，而是**同一条规则在四条写入通道上是否一致**：
页面表单 / 规格书提交 / 表格导入 / 批量接口。上一轮修的 F-01~F-04 与本轮修的
N-01~N-03 都是同一个根因的不同症状，各自散落在某一条通道里。

  编号  症状
  N-01  只改一格 → 整页参数的出处与置信度被改写
  N-02  导入/规格书提交不校验 applies_to → 写出永远不显示的参数
  N-03  批量接口不钳制 confidence → 落库 5.0 / -3.0
  N-04  重复建同名公司 → 201 + 一条假的新建日志
  N-05  变更日志按 UTC 显示（19:27 的改动显示成 11:27）
  N-06  勾选「已复核」不留日志
  N-07  /api/v1/products 的 meta.total 是本页条数
  N-10  导入 N+1：200 行 406 条 SQL
  N-11  重复导入同一份文件 → 清空人工复核 + 来源表膨胀
  N-12  导入报错行号与文件真实行号不一致
  N-14  CSV/Excel 优劣方向口径、/healthz 版本、默认视图上限
  N-15  出处链接未校验协议

两种跑法都支持：pytest tests/ 或 python tests/test_channel_parity.py
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-parity-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.audit import AuditLog, parse_changes  # noqa: E402
from app.compare import format_number, metric_applies  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.importer import import_rows, import_xlsx  # noqa: E402
from app.main import _default_selection, app  # noqa: E402
from app.models import Entity, Product, ProductSpec, SpecMetric, SpecSource  # noqa: E402
from app.seed import seed  # noqa: E402
from app.timeutil import fmt_local  # noqa: E402

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


def _bearer(scopes=("read", "write", "ingest", "admin")) -> dict:
    if "bearer" not in _state:
        c = _admin()
        r = c.post("/api/v1/tokens", json={"name": "一致性测试", "scopes": list(scopes)})
        assert r.status_code == 201, r.text
        _state["bearer"] = {"Authorization": f"Bearer {r.json()['data']['token']}"}
    return _state["bearer"]  # type: ignore[return-value]


def _product(model: str) -> Product:
    with Session(engine) as s:
        p = s.exec(select(Product).where(Product.model == model)).first()
        assert p is not None, f"找不到型号 {model}"
        s.expunge(p)
        return p


def _metric(s: Session, key: str) -> SpecMetric:
    m = s.exec(select(SpecMetric).where(SpecMetric.key == key)).first()
    assert m is not None, f"找不到指标 {key}"
    return m


def _specs(product_id: int) -> dict[int, ProductSpec]:
    with Session(engine) as s:
        return {sp.metric_id: sp for sp in s.exec(
            select(ProductSpec).where(ProductSpec.product_id == product_id)
        ).all()}


def _specs_form(product_id: int) -> dict[str, str]:
    """按浏览器语义组装参数表单：值/条件/复核勾选全部回帖。"""
    form: dict[str, str] = {"_csrf": _csrf(f"/products/{product_id}")}
    with Session(engine) as s:
        p = s.get(Product, product_id)
        assert p is not None
        pool = [m for m in s.exec(select(SpecMetric)).all()
                if metric_applies(m, p.category)]
        current = {sp.metric_id: sp for sp in s.exec(
            select(ProductSpec).where(ProductSpec.product_id == product_id)
        ).all()}
        for m in pool:
            sp = current.get(m.id)  # type: ignore[arg-type]
            value = ""
            if sp is not None:
                if m.value_type == "number" and sp.value_num is not None:
                    value = format_number(sp.value_num, m.precision)
                else:
                    value = sp.value_text or ""
            form[f"{m.id}:v"] = value
            form[f"{m.id}:c"] = (sp.condition or "") if sp else ""
            if sp is not None and sp.verified:
                form[f"{m.id}:ok"] = "on"
    return form


# ── N-01 只改一格，不该动别的行的出处 ─────────────────────────────────────
def test_save_one_cell_keeps_other_rows_source():
    c = _admin()
    p = _product("MZB4001T05")
    with Session(engine) as s:
        flow = _metric(s, "flow_air")
        before = {m: (sp.source_id, sp.confidence) for m, sp in _specs(p.id).items()}

    form = _specs_form(p.id)
    form[f"{flow.id}:v"] = "0.51"
    r = c.post(f"/products/{p.id}/specs", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text

    with Session(engine) as s:
        after = {m: (sp.source_id, sp.confidence) for m, sp in _specs(p.id).items()}
        changed_src = s.get(SpecSource, after[flow.id][0])
        assert changed_src is not None and changed_src.kind == "manual"

    untouched = [m for m in before if m != flow.id and m in after]
    assert untouched, "前置条件：该型号应有多项参数"
    for mid in untouched:
        assert after[mid] == before[mid], (
            f"指标 {mid} 没被改动，出处/置信度不该变：{before[mid]} -> {after[mid]}"
        )


# ── N-02 applies_to 在四条通道上口径一致 ──────────────────────────────────
def test_import_rejects_metric_from_another_category():
    with Session(engine) as s:
        rep = import_rows(
            s,
            [{"型号": "XCAT-1", "友商": "跨类测试", "大类": "压电气泵",
              "指标": "液体流量", "数值": "8000"}],
            ["型号", "友商", "大类", "指标", "数值"],
        )
        assert rep.written_specs == 0, "跨大类指标不该写库"
        assert any("不适用" in e for e in rep.errors), rep.errors
        assert s.exec(select(Product).where(Product.model == "XCAT-1")).first() is None


def test_datasheet_commit_rejects_metric_from_another_category():
    c = _admin()
    html = c.get("/datasheet").text
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    with Session(engine) as s:
        ent = s.exec(select(Entity).where(Entity.name == "村田制作所")).first()

    r = c.post(
        "/api/datasheet/analyze",
        files={"file": ("spec.txt", "空气流量 0.7 L/min\n".encode(), "text/plain")},
        data={"entity_id": str(ent.id), "category": "pump_air", "model": "DS-XCAT-1"},
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["doc_id"]

    r = c.post(
        "/api/datasheet/commit",
        json={
            "doc_id": doc_id,
            "target": {"entity_id": ent.id, "category": "pump_air", "model": "DS-XCAT-1"},
            "rows": [{"metric_key": "flow_liquid", "value_num": 8000.0, "confidence": 0.9}],
        },
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["written"] == 0 and body["skipped"] == 1, body
    assert any("不适用" in e for e in body["errors"]), body["errors"]


# ── N-03 置信度在批量接口上也要钳制 ───────────────────────────────────────
def test_batch_clamps_confidence():
    c = _admin()
    h = _bearer()
    pid = _product("MZB3004T04").id
    r = c.post("/api/v1/specs/batch", headers=h, json={"items": [
        {"product_id": pid, "metric_key": "flow_air", "value_num": 0.166, "confidence": 5},
        {"product_id": pid, "metric_key": "power_mw", "value_num": 30, "confidence": -3},
    ]})
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        flow, power = _metric(s, "flow_air"), _metric(s, "power_mw")
        specs = _specs(pid)
        assert specs[flow.id].confidence == 1.0, specs[flow.id].confidence
        assert specs[power.id].confidence == 0.0, specs[power.id].confidence


# ── N-04 重复建同名公司 ───────────────────────────────────────────────────
def test_duplicate_entity_is_reused_not_created():
    c = _admin()
    h = _bearer()
    first = c.post("/api/v1/entities", headers=h, json={"name": "重复测试公司"})
    assert first.status_code == 201 and first.json()["meta"]["created"] is True

    with Session(engine) as s:
        count_before = len(s.exec(select(Entity).where(Entity.name == "重复测试公司")).all())
        logs_before = len(s.exec(select(AuditLog)).all())

    second = c.post("/api/v1/entities", headers=h, json={"name": "重复测试公司"})
    assert second.status_code == 200, second.text
    assert second.json()["meta"]["created"] is False

    with Session(engine) as s:
        assert len(s.exec(select(Entity).where(Entity.name == "重复测试公司")).all()) == count_before
        assert len(s.exec(select(AuditLog)).all()) == logs_before, "重复建不该再记一条新建日志"


# ── N-05 日志时间按部署时区显示 ───────────────────────────────────────────
def test_change_log_shows_local_time():
    c = _admin()
    with Session(engine) as s:
        row = s.exec(select(AuditLog).where(
            AuditLog.note == "时区测试")).first()
        if row is None:
            from app import audit

            audit.record(s, actor="admin", action="update", object_type="setting",
                         label="时区测试", changes=[{"field": "x", "old": "a", "new": "b"}],
                         note="时区测试")
            s.commit()
            row = s.exec(select(AuditLog).where(AuditLog.note == "时区测试")).first()
        assert row is not None
        stamp = row.at
        expected = fmt_local(stamp, "%m-%d %H:%M:%S")

    html = c.get("/admin/logs?days=1").text
    assert expected in html, f"日志页应显示本地时间 {expected}（库里存的是 UTC）"
    utc_text = f"{stamp.strftime('%m-%d %H:%M:%S')}"
    if utc_text != expected:
        assert utc_text not in html, "不该把 UTC 直接当本地时间打印"


# ── N-06 复核勾选要留痕 ───────────────────────────────────────────────────
def test_verify_toggle_is_audited():
    c = _admin()
    p = _product("MZB3005T06")
    with Session(engine) as s:
        mid = _metric(s, "flow_air").id
        sp = _specs(p.id)[mid]
        sp.verified = False
        s.add(sp)
        s.commit()

    form = _specs_form(p.id)
    form[f"{mid}:ok"] = "on"
    r = c.post(f"/products/{p.id}/specs", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text

    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(
            AuditLog.object_type == "spec", AuditLog.object_id == p.id
        ).order_by(AuditLog.id.desc())).all()
        assert rows, "只改复核状态也必须留一条日志"
        fields = [c.get("field", "") for c in parse_changes(rows[0])]
        assert any("复核" in f for f in fields), fields


# ── N-07 分页元信息 ───────────────────────────────────────────────────────
def test_products_listing_meta_is_page_not_total():
    c = _admin()
    r = c.get("/api/v1/products?limit=3", headers=_bearer())
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    assert meta["count"] == 3 and meta["has_more"] is True, meta
    assert "total" not in meta, "total 曾是本页条数，语义误导，已改为 count + has_more"


# ── N-10 导入不再是每行一次查询 ───────────────────────────────────────────
def test_import_query_count_does_not_scale_with_rows():
    counter = {"n": 0, "select": 0}

    def _hook(conn, cursor, statement, params, context, executemany):
        counter["n"] += 1
        if statement.lstrip().upper().startswith("SELECT"):
            counter["select"] += 1

    rows = []
    for i in range(10):
        for key in ("flow_air", "pressure_air", "resonance", "power_mw", "size"):
            rows.append({"型号": f"PERF-{i}", "友商": "性能测试", "大类": "压电气泵",
                         "指标": key, "数值": "1.0"})

    event.listen(engine, "before_cursor_execute", _hook)
    try:
        with Session(engine) as s:
            rep = import_rows(s, rows, ["型号", "友商", "大类", "指标", "数值"])
    finally:
        event.remove(engine, "before_cursor_execute", _hook)

    assert rep.written_specs == len(rows), rep.as_dict()
    # 旧实现每行一次 select(ProductSpec)：50 行 → 50 条 SELECT，总量约 2 条/行。
    # 现在按型号预加载，SELECT 只随「不同型号数」增长（10 个型号 ≈ 14 条）。
    assert counter["select"] <= 15, f"50 行导入用了 {counter['select']} 条 SELECT"
    assert counter["n"] <= 2 * len(rows), f"50 行导入共用了 {counter['n']} 条 SQL"


# ── N-11 重复导入是安全的 ─────────────────────────────────────────────────
def test_reimport_unchanged_row_is_noop():
    rows = [{"型号": "IDEMP-1", "友商": "幂等测试", "大类": "压电气泵",
             "指标": "空气流量", "数值": "0.42", "条件": "@15Vpp"}]
    headers = ["型号", "友商", "大类", "指标", "数值", "条件"]
    with Session(engine) as s:
        first = import_rows(s, rows, headers)
        assert first.written_specs == 1, first.as_dict()
        p = s.exec(select(Product).where(Product.model == "IDEMP-1")).first()
        assert p is not None
        pid = p.id
        mid = _metric(s, "flow_air").id
        sp = s.exec(select(ProductSpec).where(
            ProductSpec.product_id == pid, ProductSpec.metric_id == mid)).first()
        assert sp is not None
        sp.verified = True
        s.add(sp)
        s.commit()
        sources_before = len(s.exec(select(SpecSource)).all())

    with Session(engine) as s:
        again = import_rows(s, rows, headers)
        assert again.written_specs == 0, again.as_dict()
        assert again.skipped_unchanged == 1, again.as_dict()
        sp = s.exec(select(ProductSpec).where(
            ProductSpec.product_id == pid, ProductSpec.metric_id == mid)).first()
        assert sp is not None and sp.verified is True, "值没变就不该清掉人工复核"
        assert len(s.exec(select(SpecSource)).all()) == sources_before, "重复导入不该再造来源行"


# ── N-12 报错行号 = 文件真实行号 ──────────────────────────────────────────
def test_import_error_line_number_matches_file():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["某某公司型号参数表"])                                # 第 1 行：标题
    ws.append(["型号", "友商", "大类", "指标", "数值"])              # 第 2 行：表头
    ws.append(["LINE-1", "行号测试", "压电气泵", "空气流量", "0.5"])  # 第 3 行：正常
    ws.append(["", "行号测试", "压电气泵", "空气流量", "0.6"])        # 第 4 行：型号为空
    buf = io.BytesIO()
    wb.save(buf)

    with Session(engine) as s:
        rep = import_xlsx(s, buf.getvalue(), batch_note="行号测试.xlsx")
    assert rep.written_specs == 1, rep.as_dict()
    assert any("第 4 行" in e for e in rep.errors), rep.errors


# ── N-14 口径与小改动 ─────────────────────────────────────────────────────
def test_healthz_reports_version_csv_uses_chinese_labels_default_view_capped():
    c = _admin()
    body = c.get("/healthz").json()
    assert body["ok"] is True and body.get("version"), body

    csv_text = c.get("/export.csv").text
    assert "越大越好" in csv_text and "lower" not in csv_text, "CSV 的优劣方向应与 Excel 同口径"

    with Session(engine) as s:
        products = list(s.exec(select(Product)).all())
    assert len(_default_selection(products, limit=8)) <= 4, "首屏默认最多 4 列"
    assert len(_default_selection(products, "pump_air", limit=8)) <= 4


# ── N-15 出处链接的协议白名单 ─────────────────────────────────────────────
def test_javascript_url_is_rejected_everywhere():
    c = _admin()
    p = _product("MZB1001T02")
    form = {"_csrf": _csrf(f"/products/{p.id}"), "model": p.model,
            "name": p.name or "", "category": p.category, "status": p.status,
            "datasheet_url": "javascript:alert(1)", "note": ""}
    r = c.post(f"/products/{p.id}", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    with Session(engine) as s:
        assert s.get(Product, p.id).datasheet_url is None, "javascript: 不该存进库"

    r = c.patch(f"/api/v1/products/{p.id}", headers=_bearer(),
                json={"datasheet_url": "data:text/html,<script>1</script>"})
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        assert s.get(Product, p.id).datasheet_url is None

    # 正常值仍然照收，裸域名补 https
    r = c.patch(f"/api/v1/products/{p.id}", headers=_bearer(),
                json={"datasheet_url": "www.murata.com/zh-cn"})
    with Session(engine) as s:
        assert s.get(Product, p.id).datasheet_url == "https://www.murata.com/zh-cn"


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
