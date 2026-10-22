"""单位归一（`app/units.py`）与「导入时按单位列换算」的单测。

守的是几条会让数据差几个量级的规则：
  · 同维度才换算（mL/min → L/min、mbar → kPa），跨维度一律拒绝并说清原因
  · Vp → Vpp 是定义性的 ×2，直接换；±25V 这类峰值写法同样 ×2
  · Vrms、只写「V」的电压不猜（波形/口径不明），原样保留并进「待确认」清单
  · Vdc 不参与 Vpp 换算 —— 直流乘 2 变成「峰峰值」没有物理意义
  · 批量导入的「单位」列真的会被读、被换算；认不出的单位让整行报错跳过，
    而不是把 1100 mL/min 原样写成 1100 L/min

两种跑法都支持：pytest tests/ 或 python tests/test_units.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-units-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _wide import wide_csv
from app import units
from app.db import engine, init_db
from app.importer import import_csv
from app.models import Product, ProductSpec, SpecMetric
from app.seed import seed
from sqlmodel import Session, select

_state: dict[str, object] = {}


def _session() -> Session:
    if not _state.get("ready"):
        init_db()
        seed()
        _state["ready"] = True
    return Session(engine)


def _close(s: Session) -> None:
    s.close()


def _csv(**cells) -> str:
    """按宽表拼一行（一行一个型号，指标是列）。"""
    return wide_csv({"企业名称": "单位测试公司", "产品类别": "压电气泵", **cells})


def _product(model: str) -> Product | None:
    with Session(engine) as s:
        return s.exec(select(Product).where(Product.model == model)).first()


def _spec(model: str, metric_key: str) -> ProductSpec | None:
    with Session(engine) as s:
        p = s.exec(select(Product).where(Product.model == model)).first()
        assert p is not None, f"型号 {model} 没建出来"
        m = s.exec(select(SpecMetric).where(SpecMetric.key == metric_key)).first()
        assert m is not None
        return s.exec(
            select(ProductSpec).where(
                ProductSpec.product_id == p.id, ProductSpec.metric_id == m.id
            )
        ).first()


# ── 换算表 ─────────────────────────────────────────────────────────────────
def test_flow_ml_min_to_l_min():
    r = units.convert(1100, "mL/min", "L/min")
    assert r.ok and r.converted
    assert abs(r.value - 1.1) < 1e-9, f"1100 mL/min 应该是 1.1 L/min，得到 {r.value}"


def test_flow_ul_min_to_l_min():
    r = units.convert(8000, "µL/min", "L/min")
    assert r.ok and abs(r.value - 0.008) < 1e-12


def test_pressure_mbar_to_kpa():
    r = units.convert(500, "mbar", "kPa")
    assert r.ok and abs(r.value - 50.0) < 1e-9, f"500 mbar 应该是 50 kPa，得到 {r.value}"


def test_pressure_pa_to_kpa_and_mpa():
    assert abs(units.convert(1200, "Pa", "kPa").value - 1.2) < 1e-9
    assert abs(units.convert(0.25, "MPa", "kPa").value - 250.0) < 1e-9


def test_power_and_frequency_and_length():
    assert abs(units.convert(2.5, "W", "mW").value - 2500.0) < 1e-9
    assert abs(units.convert(24000, "Hz", "kHz").value - 24.0) < 1e-9
    assert abs(units.convert(2.1, "cm", "mm").value - 21.0) < 1e-9


def test_same_unit_is_not_a_conversion():
    r = units.convert(0.7, "L/min", "L/min")
    assert r.ok and not r.converted and r.value == 0.7


def test_cross_dimension_is_rejected_with_reason():
    r = units.convert(1.0, "kPa", "L/min")
    assert not r.ok, "压力换流量必须被拒，不能「取个近似」"
    assert r.reason and "跨维度" in r.reason


def test_unknown_unit_is_rejected():
    r = units.convert(5.0, "furlong", "mm")
    assert not r.ok and "不认识单位" in (r.reason or "")


# ── 电压文本归一 ───────────────────────────────────────────────────────────
def test_voltage_pp_keeps_value():
    r = units.normalize_voltage_text("50Vpp")
    assert r.text == "50 Vpp" and r.converted and not r.needs_review


def test_voltage_peak_doubles():
    r = units.normalize_voltage_text("11.5Vp")
    assert r.text == "23 Vpp", f"Vp → Vpp 应乘 2，得到 {r.text}"


def test_voltage_pm_amplitude_doubles():
    r = units.normalize_voltage_text("±25V")
    assert r.text == "50 Vpp", f"±25V 是 ±25V 峰值，即 50 Vpp，得到 {r.text}"


def test_voltage_range_is_converted_end_to_end():
    r = units.normalize_voltage_text("11.5~21 Vp")
    assert r.text == "23~42 Vpp", r.text


def test_voltage_millivolt_pp_is_scaled():
    r = units.normalize_voltage_text("30mVpp")
    assert r.text == "0.03 Vpp", r.text


def test_voltage_parenthetical_condition_is_kept():
    r = units.normalize_voltage_text("50Vpp (±25V Square wave)")
    assert r.text.startswith("50 Vpp")
    assert "Square wave" in r.text, "括注里的测试条件不能丢"


def test_voltage_rms_needs_review():
    r = units.normalize_voltage_text("3.3Vrms")
    assert r.needs_review and not r.converted
    assert r.text == "3.3Vrms", "口径不明就原样保留，不能猜波形"


def test_voltage_bare_v_needs_review():
    r = units.normalize_voltage_text("20 V")
    assert r.needs_review and not r.converted and r.text == "20 V"
    r2 = units.normalize_voltage_text("250")
    assert r2.needs_review and r2.text == "250"


def test_voltage_dc_is_not_doubled():
    r = units.normalize_voltage_text("11.5~21Vdc")
    assert not r.converted and "Vdc" in r.text, "直流乘 2 变成峰峰值没有物理意义"


# ── 导入通道 ───────────────────────────────────────────────────────────────
def test_csv_import_converts_unit():
    s = _session()
    try:
        rep = import_csv(s, _csv(**{"产品型号": "IMP-A", "流量L/min": "1100 mL/min"}))
        s.commit()
        assert not rep.errors, rep.errors
        assert rep.converted_units == 1
        assert rep.conversions and "1.1 L/min" in rep.conversions[0], rep.conversions
        spec = _spec("IMP-A", "flow_air")
        assert spec is not None
        assert abs((spec.value_num or 0) - 1.1) < 1e-9, f"落库值应为 1.1，得到 {spec.value_num}"
    finally:
        _close(s)


def test_csv_import_rejects_cross_dimension_unit():
    s = _session()
    try:
        rep = import_csv(s, _csv(**{"产品型号": "IMP-B", "流量L/min": "1.5 kPa"}))
        s.commit()
        assert rep.errors and "跨维度" in rep.errors[0], rep.errors
        assert _product("IMP-B") is None, "整行只有非法值，连型号都不该建出来"
    finally:
        _close(s)


def test_csv_import_normalizes_voltage_text():
    s = _session()
    try:
        rep = import_csv(s, wide_csv(
            {"企业名称": "单位测试公司", "产品类别": "压电气泵",
             "产品型号": "IMP-C", "驱动电压": "±25V"},
            {"企业名称": "单位测试公司", "产品类别": "压电气泵",
             "产品型号": "IMP-D", "驱动电压": "3.3Vrms"},
        ))
        s.commit()
        spec = _spec("IMP-C", "drive_voltage")
        assert spec is not None and spec.value_text == "50 Vpp", spec.value_text
        # 口径不明的进待确认清单，而不是被悄悄换算
        assert any("Vrms" in x for x in rep.needs_review), rep.needs_review
        assert _spec("IMP-D", "drive_voltage").value_text == "3.3Vrms"
    finally:
        _close(s)


def test_import_without_unit_column_mean_is_metric_unit():
    s = _session()
    try:
        rep = import_csv(s, _csv(**{"产品型号": "IMP-E", "流量L/min": "0.7"}))
        s.commit()
        assert not rep.errors and rep.converted_units == 0
        assert abs((_spec("IMP-E", "flow_air").value_num or 0) - 0.7) < 1e-12
    finally:
        _close(s)


# ── 未公开的写法与「单位就是字典单位」─────────────────────────────────────
def test_temperature_cell_accepts_metric_own_unit():
    """「0~45 ℃」必须落成 45 ℃。

    ℃ 刻意不在换算表里（℉↔℃ 要偏移量，不是纯因子），于是「认不出的单位就报错」
    那条护栏把整列温度全拦了下来 —— 单位写的就是字典自己的单位，根本不需要换算。
    """
    s = _session()
    try:
        rep = import_csv(s, _csv(**{"产品型号": "TEMP-1", "温度范围": "-40~+85 ℃"}))
        assert rep.errors == [], rep.errors
        hi = _spec("TEMP-1", "op_temp_max")
        lo = _spec("TEMP-1", "op_temp_min")
        assert hi is not None and hi.value_num == 85, hi
        # 温度是范围：一格落两格，下限不能丢（-40 与 0 的低温能力差 40 ℃）
        assert lo is not None and lo.value_num == -40, lo
        assert hi.condition == "原文 -40~+85" and lo.condition == hi.condition
    finally:
        _close(s)


def test_single_temperature_value_writes_only_upper_bound():
    """单点温度（如「60 ℃」）没有下限可推，只写上限 —— 不硬凑成 60~60。"""
    s = _session()
    try:
        rep = import_csv(s, _csv(**{"产品型号": "TEMP-2", "温度范围": "60 ℃"}))
        assert rep.errors == [], rep.errors
        assert _spec("TEMP-2", "op_temp_max").value_num == 60
        assert _spec("TEMP-2", "op_temp_min") is None
    finally:
        _close(s)


def test_missing_with_parenthetical_note_is_not_an_error():
    """「待补充（官方仅给风速 1.4–1.8 m/s）」是未公开，不是要解析的数字。

    括注是给人看的补充说明。当成值去解析会报「单位 m/s 认不出」，
    看起来像数据出错，实际是这格本来就没公开。
    """
    s = _session()
    try:
        rep = import_csv(s, _csv(**{
            "产品型号": "MISS-1",
            "流量L/min": "待补充（官方仅给风速 1.4–1.8 m/s）",
        }))
        assert rep.errors == [], rep.errors
        assert rep.written_specs == 0
        assert rep.skipped_missing == 1, rep.as_dict()
        # 整行只有一格「未公开」，不该顺手建出一个空型号
        assert _product("MISS-1") is None

        # 文本型指标的同类写法相反：原文就是值，括注里有信息，必须存下来
        rep2 = import_csv(s, _csv(**{
            "产品型号": "MISS-2", "外形尺寸": "未公开（全系列约 7 cm³）",
        }))
        assert rep2.errors == [], rep2.errors
        assert rep2.written_specs == 1, rep2.as_dict()
        assert _spec("MISS-2", "size").value_text == "未公开（全系列约 7 cm³）"
    finally:
        _close(s)


def test_unknown_unit_is_still_rejected():
    """上面两条放宽不能把护栏拆掉：phon 是真换不了的单位（声压级口径不同），必须报错。"""
    s = _session()
    try:
        rep = import_csv(s, _csv(**{"产品型号": "PHON-1", "噪音": "＜10 phon"}))
        assert any("phon" in e for e in rep.errors), rep.errors
        assert rep.written_specs == 0
        assert _product("PHON-1") is None, "整行只有非法值，不该建出空型号"
    finally:
        _close(s)


def test_parse_range_reads_both_ends():
    """「原文 -40~+85」这类文字要能取出 (下限, 上限)，顺序反了也要归位。

    三条通道（表格导入、规格书确认写入、v5 迁移）都靠它，解析口径必须只有一处。
    """
    from app import range_pair

    assert range_pair.parse_range("原文 -40~+85") == (-40.0, 85.0)
    assert range_pair.parse_range("原文 +85~-40") == (-40.0, 85.0), "原文顺序反了也要归位"
    assert range_pair.parse_range("0至60") == (0.0, 60.0)
    assert range_pair.parse_range("原文 0~+65（使用）／-40~+65（存储）") == (0.0, 65.0)
    assert range_pair.parse_range("≥0.7 L/min") is None, "没有区间不能硬解析"
    assert range_pair.parse_range(None) is None
    assert range_pair.lower_metric_key("op_temp_max") == "op_temp_min"
    assert range_pair.lower_metric_key("flow_air") is None


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
