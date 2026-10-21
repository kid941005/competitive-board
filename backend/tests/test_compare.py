"""参数对比逻辑的单测。

两种跑法都支持：
    pytest tests/
    python tests/test_compare.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _wide import wide_csv
from app.compare import (
    build_matrix,
    compare_key,
    completeness,
    format_number,
    metrics_for_categories,
)
from app.importer import import_csv
from app.models import Entity, Product, ProductSpec, SpecMetric
from sqlmodel import Session, SQLModel, create_engine, select


# ── 夹具 ───────────────────────────────────────────────────────────────────
def _metric(**kw) -> SpecMetric:
    base = dict(
        id=1, key="insertion_loss", category="射频性能", name="插入损耗",
        unit="dB", value_type="number", better="lower", precision=2, display_order=10,
    )
    base.update(kw)
    return SpecMetric(**base)


def _product(pid: int, model: str) -> Product:
    return Product(id=pid, entity_id=1, model=model)


def _spec(pid: int, mid: int, num: float | None = None, text: str | None = None, sid: int = 1) -> ProductSpec:
    return ProductSpec(id=sid, product_id=pid, metric_id=mid, value_num=num, value_text=text)


# ── 格式化 ─────────────────────────────────────────────────────────────────
def test_format_number():
    assert format_number(1.5, 2) == "1.50"
    assert format_number(1200, 0) == "1200"
    assert format_number(0.456, 1) == "0.5"


# ── 最优值 ─────────────────────────────────────────────────────────────────
def test_best_lower_is_better():
    m = _metric(better="lower")
    rows = build_matrix(
        [_product(1, "A"), _product(2, "B")],
        [m],
        [_spec(1, 1, 1.5), _spec(2, 1, 1.8)],
    )
    assert rows[0].best == {1}
    assert rows[0].cells[1].is_best is True
    assert rows[0].cells[2].is_best is False


def test_best_higher_is_better():
    m = _metric(key="rejection", name="带外抑制", better="higher", precision=0)
    rows = build_matrix(
        [_product(1, "A"), _product(2, "B")],
        [m],
        [_spec(1, 1, 40), _spec(2, 1, 35)],
    )
    assert rows[0].best == {1}


def test_best_tie_returns_all():
    m = _metric(better="lower")
    rows = build_matrix(
        [_product(1, "A"), _product(2, "B"), _product(3, "C")],
        [m],
        [_spec(1, 1, 1.5), _spec(2, 1, 1.5), _spec(3, 1, 2.0)],
    )
    assert rows[0].best == {1, 2}


def test_best_needs_two_values():
    """只有一个产品有值时不给最优 —— 那是「唯一」，不是「最优」。"""
    m = _metric(better="lower")
    rows = build_matrix([_product(1, "A"), _product(2, "B")], [m], [_spec(1, 1, 1.5)])
    assert rows[0].best == set()


def test_best_skipped_for_target_metric():
    m = _metric(key="center_freq", name="中心频率", better="target", precision=0)
    rows = build_matrix(
        [_product(1, "A"), _product(2, "B")],
        [m],
        [_spec(1, 1, 1200), _spec(2, 1, 2400)],
    )
    assert rows[0].best == set()


def test_best_skipped_for_text_metric():
    m = _metric(key="package", name="封装形式", value_type="text", better="na")
    rows = build_matrix(
        [_product(1, "A"), _product(2, "B")],
        [m],
        [_spec(1, 1, text="SMD 1109"), _spec(2, 1, text="SMD 1411")],
    )
    assert rows[0].best == set()
    assert rows[0].differs is True


# ── 缺失处理 ───────────────────────────────────────────────────────────────
def test_missing_is_dash_not_zero():
    m = _metric(better="lower")
    rows = build_matrix([_product(1, "A"), _product(2, "B")], [m], [_spec(1, 1, 1.5)])
    cell = rows[0].cells[2]
    assert cell.is_missing is True
    assert cell.display == "—"
    assert cell.value_num is None
    assert rows[0].has_missing is True
    # 缺失不算「值不同」，但会走 has_missing 通道
    assert rows[0].differs is False


def test_missing_not_counted_as_best():
    m = _metric(better="lower")
    rows = build_matrix(
        [_product(1, "A"), _product(2, "B"), _product(3, "C")],
        [m],
        [_spec(1, 1, 1.8), _spec(2, 1, 1.5)],
    )
    assert rows[0].best == {2}
    assert rows[0].cells[3].is_missing is True


# ── 差异判定与容差 ─────────────────────────────────────────────────────────
def test_differs_with_tolerance():
    m = _metric(better="lower", precision=2)
    rows = build_matrix(
        [_product(1, "A"), _product(2, "B")],
        [m],
        [_spec(1, 1, 1.5), _spec(2, 1, 1.50000001)],
    )
    assert rows[0].differs is False


def test_differs_detects_real_change():
    m = _metric(better="lower", precision=2)
    rows = build_matrix(
        [_product(1, "A"), _product(2, "B")],
        [m],
        [_spec(1, 1, 1.5), _spec(2, 1, 1.8)],
    )
    assert rows[0].differs is True


def test_compare_key_text_normalized():
    m = _metric(value_type="text", better="na")
    rows = build_matrix(
        [_product(1, "A"), _product(2, "B")],
        [m],
        [_spec(1, 1, text="SMD 1109"), _spec(2, 1, text="smd 1109")],
    )
    assert compare_key(rows[0].cells[1], m) == compare_key(rows[0].cells[2], m)
    assert rows[0].differs is False


# ── 完整度 ─────────────────────────────────────────────────────────────────
def test_completeness():
    m1 = _metric(id=1, key="a", name="A", better="lower")
    m2 = _metric(id=2, key="b", name="B", better="higher")
    m3 = _metric(id=3, key="c", name="C", better="higher")
    rows = build_matrix(
        [_product(1, "A"), _product(2, "B")],
        [m1, m2, m3],
        [_spec(1, 1, 1.0), _spec(1, 2, 2.0), _spec(2, 3, 3.0)],
    )
    comp = completeness(rows, [1, 2])
    assert abs(comp[1] - 2 / 3) < 1e-9
    assert abs(comp[2] - 1 / 3) < 1e-9


# ── 指标按产品大类裁剪 ─────────────────────────────────────────────────────
def test_metrics_for_categories():
    """陶瓷与压电泵的参数体系不通用，跨类对比必须裁掉无关指标行。"""
    general = _metric(id=1, key="g", name="通用", applies_to="")
    ceramic = _metric(id=2, key="c", name="陶瓷专用", applies_to="ceramic")
    pump = _metric(id=3, key="p", name="泵风扇共用", applies_to="pump_air,fan")
    pool = [general, ceramic, pump]

    assert [m.key for m in metrics_for_categories(pool, {"ceramic"})] == ["g", "c"]
    assert [m.key for m in metrics_for_categories(pool, {"fan"})] == ["g", "p"]
    assert [m.key for m in metrics_for_categories(pool, {"pump_liquid"})] == ["g"]
    # 未选产品时只保留通用指标
    assert [m.key for m in metrics_for_categories(pool, set())] == ["g"]


def test_metrics_for_categories_multi_select():
    """同时选陶瓷 + 压电气泵，两类指标都要出现。"""
    ceramic = _metric(id=2, key="c", name="陶瓷", applies_to="ceramic")
    pump = _metric(id=3, key="p", name="气泵", applies_to="pump_air")
    got = metrics_for_categories([ceramic, pump], {"ceramic", "pump_air"})
    assert [m.key for m in got] == ["c", "p"]


# ── 表格导入（宽表）────────────────────────────────────────────────────────
def _import_session() -> Session:
    """导入用的内存库。

    宽表的列头直接绑指标 key，所以这里必须放**真实**的字典项（不能用合成指标），
    否则列头找不到对应指标，任何断言都测不到东西。
    """
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    s = Session(engine)
    s.add(SpecMetric(id=1, key="flow_air", category="气动与流体", name="空气流量",
                     unit="L/min", value_type="number", better="higher", precision=3,
                     applies_to="pump_air"))
    s.add(SpecMetric(id=2, key="pressure_air", category="气动与流体", name="静压",
                     unit="kPa", value_type="number", better="higher", precision=2,
                     applies_to="pump_air"))
    s.add(SpecMetric(id=3, key="drive_voltage", category="驱动与功耗", name="驱动电压",
                     unit="V", value_type="text", better="na", precision=0,
                     applies_to="pump_air"))
    s.add(Entity(id=1, kind="competitor", name="华芯微电子"))
    s.commit()
    return s


def test_import_csv_ok():
    s = _import_session()
    text = wide_csv({
        "企业名称": "华芯微电子", "产品型号": "SX-2400", "产品类别": "压电气泵",
        "流量L/min": "0.7", "静压Pa": "1420", "来源链接": "https://x/1.pdf", "置信度": "0.95",
    })
    rep = import_csv(s, text)
    assert rep.errors == [], rep.errors
    assert rep.written_specs == 2
    assert rep.created_products == 1
    # 列头单位 Pa → 字典单位 kPa
    assert rep.converted_units == 1, rep.conversions


def test_import_csv_unknown_column_is_ignored():
    s = _import_session()
    text = (
        "企业名称,产品型号,产品类别,流量L/min,瞎编的指标\n"
        "华芯微电子,SX-1,压电气泵,0.5,3\n"
    )
    rep = import_csv(s, text)
    assert rep.written_specs == 1, rep.as_dict()
    assert rep.errors == []


def test_import_csv_missing_required_header():
    s = _import_session()
    rep = import_csv(s, "产品型号,流量L/min\nSX-1,0.5\n")
    assert rep.written_specs == 0
    assert any("宽表" in e for e in rep.errors), rep.errors


def test_import_csv_empty_value_skipped():
    """「待补充」算未公开：跳过不写、计入 skipped_missing；真空白只是「这列没用到」，不计。"""
    s = _import_session()
    text = wide_csv({"企业名称": "华芯微电子", "产品型号": "SX-1", "产品类别": "压电气泵",
                     "流量L/min": "待补充"})
    rep = import_csv(s, text)
    assert rep.skipped_missing == 1, rep.as_dict()
    assert rep.written_specs == 0
    assert rep.errors == []


def test_import_csv_text_metric_keeps_original_text():
    """文本型指标（驱动电压）存原文，但电压口径会被统一成 Vpp。"""
    s = _import_session()
    text = wide_csv({"企业名称": "华芯微电子", "产品型号": "SX-1", "产品类别": "压电气泵",
                     "驱动电压": "±25V"})
    rep = import_csv(s, text)
    assert rep.errors == [], rep.errors
    assert rep.written_specs == 1, rep.as_dict()
    spec = s.exec(select(ProductSpec).where(ProductSpec.metric_id == 3)).first()
    assert spec is not None and spec.value_text == "50 Vpp", spec.value_text


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
