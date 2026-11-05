"""数据完整性与关键入口的回归测试（不依赖 HTTP 客户端）。

这里守的是几条「出错时不会报错、只会静默丢数据或 500」的规则：

  · 变更日志的关键词只有空白时，不能把 None 拼进 LIKE；
  · `seed --force` 在已有客户采用记录时也必须能跑完（外键顺序）；
  · 导入模板的示例行必须与表头同宽，否则下载下来的模板多出两列空白；
  · 公司名归一必须与后端一致（括注不另算一家）；
  · 完整度遇到已被删除的型号 id 时降级为 0，而不是 KeyError。

本文件刻意只用同步调用：它同时充当「没有 HTTP 测试环境时」的最小回归面。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-integrity-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_PASSWORD"] = "TestPass12345"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit, settings_store
from app import seed as seed_module
from app.compare import build_matrix, completeness
from app.db import engine, init_db
from app.lookups import find_entity, find_entity_exact
from app.models import Adoption, AppSetting, AuditLog, Entity, Product, User
from app.timeutil import fmt_local
from sqlmodel import Session, select

init_db()


def _user() -> User:
    """不落库的调用者身份：路由函数直接调用时只需要 username。"""
    return User(username="tester", password_hash="x", role="admin")


def test_import_template_matches_header_width() -> None:
    from app.importer import TEMPLATE_EXAMPLE, WIDE_HEADERS

    for index, row in enumerate(TEMPLATE_EXAMPLE):
        assert len(row) == len(WIDE_HEADERS), (
            f"模板第 {index + 1} 行有 {len(row)} 列，表头是 {len(WIDE_HEADERS)} 列"
        )


def test_audit_query_ignores_blank_keyword() -> None:
    with Session(engine) as session:
        _rows, total = audit.query(session, q="   ")
        assert total >= 0


def test_completeness_tolerates_unknown_product() -> None:
    rows = build_matrix([], [], [])
    assert completeness(rows, [999999]) == {999999: 0.0}


def test_company_name_normalization_ignores_parenthetical() -> None:
    seed_module.seed()
    with Session(engine) as session:
        exact, _similar = find_entity(session, "村田制作所（Murata）")
        assert exact is not None, "括注写法必须归入已有公司"
        assert exact.name == "村田制作所"


def test_company_create_bootstraps_and_blocks_duplicates() -> None:
    from app.routes_products import company_create

    with Session(engine) as session:
        first = company_create(
            name="测试新公司", kind="competitor", name_en="TestCo", region="中国",
            tier="A", _csrf=None, _user=_user(), session=session,
        )
        assert first.status_code == 303
        created = find_entity_exact(session, "测试新公司")
        assert created is not None and created.name_en == "TestCo" and created.tier == "A"

        # 同名（忽略空白与括注）不能再建一份
        again = company_create(
            name="测试新公司（TestCo）", kind="competitor", name_en="", region="",
            tier="B", _csrf=None, _user=_user(), session=session,
        )
        assert "err=" in again.headers["location"], again.headers


def test_adoption_progress_update_keeps_audit() -> None:
    from app.routes_products import adoption_update

    with Session(engine) as session:
        product = session.exec(select(Product)).first()
        assert product is not None
        customer = Entity(kind="customer", name="测试客户")
        session.add(customer)
        session.flush()
        row = Adoption(customer_id=customer.id, product_id=product.id, status="sample")
        session.add(row)
        session.commit()

        adoption_update(
            product_id=product.id, adoption_id=row.id, status="mass_production",
            year="2025", note="已量产", _csrf=None, _pair=(_user(), None), session=session,
        )
        session.refresh(row)
        assert row.status == "mass_production"
        assert row.year == 2025 and row.note == "已量产"
        latest = session.exec(
            select(AuditLog)
            .where(AuditLog.object_type == "adoption")
            .order_by(AuditLog.id.desc())
        ).first()
        assert latest is not None and "量产" in latest.changes


def test_product_meta_save_preserves_note_when_not_submitted() -> None:
    """保存型号元信息时，没提交备注不能把已有备注抹掉。

    回归：`product_save_meta` 的 note 默认 ""，而编辑页表单里原本没有这一格，
    于是每次点「保存信息」都会把导入写进来的「口径与备注」静默清空。
    """
    from app.routes_products import product_save_meta

    seed_module.seed()
    with Session(engine) as session:
        product = session.exec(select(Product)).first()
        assert product is not None
        product.note = "口径：静压@19.5Vdc"
        session.add(product)
        session.commit()
        pid = product.id

        def save(note: str | None) -> None:
            product_save_meta(
                product_id=pid, model=product.model, name=product.name or "",
                category=product.category, status=product.status,
                datasheet_url=product.datasheet_url or "", note=note,
                _csrf=None, _user=_user(), session=session,
            )
            session.refresh(product)

        save(None)
        assert product.note == "口径：静压@19.5Vdc", "没提交备注时不能清空"
        save("")
        assert product.note is None, "显式提交空串才表示清空"


def test_product_edit_template_has_note_field() -> None:
    template = Path(__file__).resolve().parent.parent / "app" / "templates" / "product_edit.html"
    assert 'name="note"' in template.read_text(encoding="utf-8"), "编辑页必须有备注输入框"


def test_settings_snapshot_uses_local_time() -> None:
    with Session(engine) as session:
        settings_store.update(session, "compare.max_products", 5, "tester")
        session.commit()
        snapshot = {item["spec"].key: item for item in settings_store.snapshot(session)}
        row = session.get(AppSetting, "compare.max_products")
        assert row is not None
        shown = snapshot["compare.max_products"]["updated_at"]
        assert shown == fmt_local(row.updated_at), (shown, fmt_local(row.updated_at))
        assert "T" not in (shown or ""), "展示格式应是「YYYY-MM-DD HH:MM」，不是 ISO 串"


def test_seed_force_clears_adoptions() -> None:
    """放在最后：它会重建整库业务数据。"""
    seed_module.seed()
    with Session(engine) as session:
        product = session.exec(select(Product)).first()
        assert product is not None
        customer = Entity(kind="customer", name="会被清掉的客户")
        session.add(customer)
        session.flush()
        session.add(Adoption(customer_id=customer.id, product_id=product.id, status="sample"))
        session.commit()

    seed_module.seed(force=True)

    with Session(engine) as session:
        assert session.exec(select(Adoption)).all() == [], "采用关系应随业务数据一起清空"
        assert session.exec(select(Product)).first() is not None, "示例数据应重建"


ORDER = [
    test_import_template_matches_header_width,
    test_audit_query_ignores_blank_keyword,
    test_completeness_tolerates_unknown_product,
    test_company_name_normalization_ignores_parenthetical,
    test_company_create_bootstraps_and_blocks_duplicates,
    test_adoption_progress_update_keeps_audit,
    test_product_meta_save_preserves_note_when_not_submitted,
    test_product_edit_template_has_note_field,
    test_settings_snapshot_uses_local_time,
    test_seed_force_clears_adoptions,
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
