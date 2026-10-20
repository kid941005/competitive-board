"""规格书自动识别的单测。

两种跑法都支持：
    pytest tests/
    python tests/test_datasheet.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.datasheet import (  # noqa: E402
    extract_candidates,
    metric_aliases,
    normalize_text,
)
from app.models import SpecMetric  # noqa: E402


def _m(key: str, name: str, unit: str, **kw) -> SpecMetric:
    base = dict(
        id=1, key=key, category="测试", name=name, unit=unit,
        value_type="number", better="higher", precision=2, display_order=10,
        applies_to="pump_air", aliases="",
    )
    base.update(kw)
    return SpecMetric(**base)


FLOW = _m("flow_air", "空气流量", "L/min", aliases="流量, 空气流量, flow rate")
PRESS = _m("pressure_air", "静压", "kPa", aliases="静压, 最大静压, 压力")
RESO = _m("resonance", "谐振频率", "kHz", value_type="text", better="na", aliases="谐振频率, 驱动频率")
SIZE = _m("size", "外形尺寸", "mm", value_type="text", better="na", aliases="外形尺寸, 尺寸")
POOL = [FLOW, PRESS, RESO, SIZE]


def _one(text: str, metrics=None, key: str = "flow_air"):
    cands = extract_candidates([(1, text)], metrics or POOL)
    hit = [c for c in cands if c.metric_key == key]
    return hit[0] if hit else None


# ── 归一化 ─────────────────────────────────────────────────────────────────
def test_normalize_fullwidth():
    assert normalize_text("流量：０．７") == "流量:0.7"
    assert normalize_text("静压 1.42　kPa") == "静压 1.42 kPa"


def test_aliases_include_name_and_english():
    names = metric_aliases(FLOW)
    assert "空气流量" in names
    assert "flow rate" in names
    assert "流量" in names


# ── 同行取值 ───────────────────────────────────────────────────────────────
def test_inline_value_with_unit():
    c = _one("空气流量 0.7 L/min")
    assert c is not None
    assert c.value_num == 0.7
    assert c.unit_found == "l/min"
    assert c.confidence >= 0.85
    assert c.converted is False


def test_label_and_value_on_separate_lines():
    """表头与数值分行是数据手册最常见的版式。"""
    c = _one("技术参数\n空气流量\n0.7 L/min\n静压\n1.42 kPa")
    assert c is not None
    assert c.value_num == 0.7


def test_colon_separated():
    c = _one("空气流量：0.70 L/min")
    assert c is not None
    assert c.value_num == 0.70


# ── 区间与比较符 ───────────────────────────────────────────────────────────
def test_range_becomes_text_value():
    c = _one("谐振频率 24~27 kHz", key="resonance")
    assert c is not None
    assert c.value_num is None
    assert c.value_text == "24~27"


def test_footnote_marker_is_not_a_value():
    """「空气流量(1) ≥0.7 L/min」里的 (1) 是脚注，不是取值。"""
    c = _one("空气流量(1) ≥0.7 L/min")
    assert c is not None
    assert c.value_num == 0.7
    assert c.unit_found == "l/min"


def test_numeric_metric_range_takes_upper_bound():
    """数值型指标遇到区间取上限，原文区间进条件。"""
    temp = _m("op_temp_max", "工作温度上限", "℃", aliases="工作温度")
    cands = extract_candidates([(1, "工作温度范围 0~70 ℃")], [temp])
    assert cands, "应识别出工作温度上限"
    assert cands[0].value_num == 70
    assert cands[0].condition and "0~70" in cands[0].condition


def test_comparator_goes_to_condition():
    c = _one("静压 ≥50 kPa", key="pressure_air")
    assert c is not None
    assert c.value_num == 50.0
    assert c.condition and "≥" in c.condition


# ── 单位换算 ───────────────────────────────────────────────────────────────
def test_unit_conversion_ml_to_l():
    c = _one("空气流量 700 mL/min")
    assert c is not None
    assert c.converted is True
    assert abs(c.value_num - 0.7) < 1e-9


def test_unit_conversion_mbar_to_kpa():
    c = _one("静压 14.2 mbar", key="pressure_air")
    assert c is not None
    assert c.converted is True
    assert abs(c.value_num - 1.42) < 1e-9


# ── 误配防护 ───────────────────────────────────────────────────────────────
def test_qualifier_is_flagged():
    """「压力型」不是取值标签，必须降权并打标。"""
    c = _one("MZB3004T04 压力型 50", key="pressure_air")
    assert c is not None
    assert c.qualifier is True
    assert c.confidence < 0.75


def test_text_metric_strips_trailing_unit():
    c = _one("外形尺寸 20×20×1.85 mm", key="size")
    assert c is not None
    assert c.value_text == "20×20×1.85"


def test_no_number_no_candidate():
    assert extract_candidates([(1, "空气流量 见曲线图")], POOL) == []


def test_unrelated_text_yields_nothing():
    assert extract_candidates([(1, "本产品适用于医疗与美容领域。")], POOL) == []


# ── 去重与命中计数 ─────────────────────────────────────────────────────────
def test_duplicate_occurrences_keep_best_and_count_hits():
    text = "空气流量 0.7 L/min\n备注\n空气流量 0.9 L/min"
    cands = extract_candidates([(1, text)], POOL)
    flow = [c for c in cands if c.metric_key == "flow_air"]
    assert len(flow) == 1, "同一指标只保留一条"
    assert flow[0].hits == 2


def test_longer_alias_wins_over_shorter():
    """「最大静压」应优先于「静压」匹配，且不重复产出。"""
    cands = extract_candidates([(1, "最大静压 50 kPa")], POOL)
    hits = [c for c in cands if c.metric_key == "pressure_air"]
    assert len(hits) == 1
    assert hits[0].matched_label == "最大静压"


# ── 多页 ───────────────────────────────────────────────────────────────────
def test_page_number_recorded():
    cands = extract_candidates([(1, "无关内容"), (2, "空气流量 0.7 L/min")], POOL)
    assert len(cands) == 1
    assert cands[0].page == 2


# ── 单位与别名的边界 ───────────────────────────────────────────────────────
LIQ = _m("flow_liquid", "液体流量", "µL/min", applies_to="pump_liquid",
         aliases="流量, 液体流量, flow rate")
KP = _m("kp", "平面机电耦合系数 kp", "", aliases="kp, 机电耦合系数")


def test_litre_per_minute_converts_into_microlitre_metric():
    """L/min 与 µL/min 相差 10⁶：必须换算，绝不能把 0.7 原样存成 0.7 µL/min。

    曾经的真实缺陷：UNIT_CONVERT 的键写成 "µL/min"（首字母大写），而 _convert()
    查找前会把两侧小写化，于是换算表查不到、静默返回原值，错值还带着 0.90 置信度。
    """
    cand = _one("流量 0.7 L/min", [LIQ], key="flow_liquid")
    assert cand.value_num == 700000.0, cand.value_num
    assert cand.converted is True, "必须标记为已换算"


def test_foreign_unit_next_to_number_is_rejected():
    """指标要流量，原文紧贴的却是 kPa —— 宁可不取，也不能把压力当流量。"""
    assert _one("流量 0.7 kPa", [LIQ], key="flow_liquid") is None


def test_short_ascii_alias_does_not_match_inside_word():
    """"kp" 不能命中 "kPa" 内部；中文别名不受词边界限制。"""
    assert _one("最大压力 1.42 kPa", [KP], key="kp") is None
    assert _one("kp 0.58", [KP], key="kp").value_num == 0.58


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
