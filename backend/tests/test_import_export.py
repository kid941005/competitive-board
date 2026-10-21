"""表格导入（Excel / CSV）与 Excel 导出的单测。

重点：
  · Excel 与 CSV 必须走同一套校验 —— 不能出现「CSV 拦住了、Excel 放过去」的差异
  · 大类填了就必须认得出，认识不了宁可跳过也不猜
  · 导出是给人看的：最优值要加粗、缺失要是「—」、完整度要能对上

两种跑法都支持：pytest tests/ 或 python tests/test_import_export.py
使用独立临时数据目录，不碰开发库。
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-sheet-test-")
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
from openpyxl import Workbook, load_workbook
from sqlmodel import Session, select

HEADER = ["型号", "友商", "大类", "指标", "数值", "单位", "条件", "来源链接", "置信度"]
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
    html = c.get("/import").text
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def _xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _post(c: TestClient, name: str, data: bytes, mime: str) -> dict:
    r = c.post(
        "/api/import",
        files={"file": (name, data, mime)},
        headers={"X-CSRF-Token": _csrf(c)},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── Excel 导入 ─────────────────────────────────────────────────────────────
def test_import_xlsx_creates_company_product_and_specs():
    c = _client()
    data = _xlsx([
        HEADER,
        ["XL-100", "表格测试甲", "压电气泵", "空气流量", 0.42, "L/min", "@15Vpp", "https://x/1.pdf", 0.9],
        ["XL-100", "表格测试甲", "压电气泵", "静压", 8.6, "kPa", "@15Vpp", "https://x/1.pdf", 0.9],
    ])
    rep = _post(c, "t.xlsx", data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert rep["rows_read"] == 2, rep
    assert rep["created_entities"] == 1 and rep["created_products"] == 1, rep
    assert rep["written_specs"] == 2 and rep["errors"] == [], rep

    with Session(engine) as s:
        ent = s.exec(select(Entity).where(Entity.name == "表格测试甲")).first()
        assert ent is not None
        prod = s.exec(select(Product).where(Product.model == "XL-100")).first()
        assert prod is not None and prod.category == "pump_air" and prod.entity_id == ent.id
        specs = s.exec(select(ProductSpec).where(ProductSpec.product_id == prod.id)).all()
        assert len(specs) == 2
        assert all(x.verified is False for x in specs), "导入的参数必须待复核"


def test_csv_and_xlsx_share_the_same_validation():
    """同一个坏行（指标不在字典）在两种格式下都必须被拦。"""
    c = _client()
    csv_text = "型号,友商,大类,指标,数值\nCSV-1,表格测试乙,压电气泵,不存在的指标,1\n"
    rep_csv = _post(c, "t.csv", csv_text.encode("utf-8"), "text/csv")

    data = _xlsx([["型号", "友商", "大类", "指标", "数值"],
                  ["XL-2", "表格测试丙", "压电气泵", "不存在的指标", 1]])
    rep_xlsx = _post(c, "t.xlsx", data,
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    assert rep_csv["written_specs"] == 0 and rep_xlsx["written_specs"] == 0
    assert "不在指标字典中" in rep_csv["errors"][0]
    assert "不在指标字典中" in rep_xlsx["errors"][0]


def test_unknown_category_is_rejected_not_guessed():
    c = _client()
    data = _xlsx([HEADER, ["BAD-1", "表格测试丁", "压电陶瓷", "空气流量", 1.0, "L/min"]])
    rep = _post(c, "t.xlsx", data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert rep["written_specs"] == 0
    assert any("不可识别" in e for e in rep["errors"]), rep["errors"]


def test_category_conflict_on_existing_product_is_skipped():
    """mp6-liq 是液泵，表里按气泵写必须报错 —— 否则参数会落到错误的指标体系。"""
    c = _client()
    data = _xlsx([HEADER, ["mp6-liq", "巴特尔斯微技术", "压电气泵", "空气流量", 0.5, "L/min"]])
    rep = _post(c, "t.xlsx", data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert rep["written_specs"] == 0
    assert any("冲突" in e for e in rep["errors"]), rep["errors"]


def test_missing_required_header_reports_clearly():
    c = _client()
    data = _xlsx([["型号", "友商"], ["X-1", "某公司"]])
    rep = _post(c, "t.xlsx", data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert rep["written_specs"] == 0
    assert any("缺少必需列" in e for e in rep["errors"])


def test_blank_value_counts_as_not_disclosed():
    c = _client()
    data = _xlsx([HEADER, ["BL-1", "表格测试戊", "压电气泵", "空气流量", None, "L/min"]])
    rep = _post(c, "t.xlsx", data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert rep["skipped_missing"] == 1 and rep["written_specs"] == 0


def test_unsupported_extension_rejected():
    c = _client()
    r = c.post("/api/import", files={"file": ("t.pdf", b"%PDF", "application/pdf")},
               headers={"X-CSRF-Token": _csrf(c)})
    assert r.status_code == 400


# ── 模板 ───────────────────────────────────────────────────────────────────
def test_xlsx_template_is_downloadable_and_parsable():
    c = _client()
    r = c.get("/api/import/template.xlsx")
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    assert "参数导入" in wb.sheetnames
    head = [wb["参数导入"].cell(1, i).value for i in range(1, 10)]
    assert head[:5] == ["型号", "友商", "大类", "指标", "数值"]
    assert "填写说明" in wb.sheetnames


# ── Excel 导出 ─────────────────────────────────────────────────────────────
def test_export_xlsx_structure_and_formatting():
    c = _client()
    # 取三个气泵型号，保证既有最优值也有缺失
    prods = c.get("/api/products").json()
    air = [p["id"] for p in prods if p["category"] == "pump_air"][:3]
    assert len(air) == 3

    r = c.get(f"/export.xlsx?p={','.join(map(str, air))}")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]

    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames[:2] == ["参数对比", "型号清单"]
    ws = wb["参数对比"]

    header = [ws.cell(1, i).value for i in range(1, 4 + len(air))]
    assert header[:4] == ["指标组", "指标", "单位", "优劣"]
    assert all("\n" in h for h in header[4:]), "型号表头应为「公司\\n型号」"

    # 找一行既有最优格又有缺失格的（各型号披露程度不同，至少要能看出这两层信息）
    missing_seen = best_seen = False
    for r_ in range(2, ws.max_row + 1):
        if ws.cell(r_, 1).value == "完整度":
            break
        vals = [ws.cell(r_, 5 + i) for i in range(len(air))]
        if any(v.value == "—" for v in vals):
            missing_seen = True
            for v in vals:
                assert v.font.color is None or v.font.color.rgb != "000000" or v.value == "—"
        if any(v.font.bold for v in vals if v.value != "—"):
            best_seen = True
    assert missing_seen, "导出里应保留缺失格（显示为「—」）"
    assert best_seen, "最优值应加粗"

    # 冻结窗格 & 完整度行
    assert ws.freeze_panes == "E2"
    labels = [ws.cell(r_, 1).value for r_ in range(2, ws.max_row + 1)]
    assert "完整度" in labels


def test_export_xlsx_respects_permission():
    """viewer 有导出权限（data:export），但不能编辑。"""
    c = _client()
    csrf = _csrf(c)
    c.post("/admin/users/create", data={
        "username": "viewer_exp", "password": "ViewerPass123",
        "display_name": "只读", "role": "viewer", "_csrf": csrf,
    }, follow_redirects=False)

    v = TestClient(app)
    v.__enter__()
    v.post("/login", data={"username": "viewer_exp", "password": "ViewerPass123"},
           follow_redirects=False)
    assert v.get("/export.xlsx").status_code == 200
    assert v.get("/products").status_code == 403
    assert v.get("/import").status_code == 403


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
