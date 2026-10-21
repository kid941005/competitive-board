"""单位归一的唯一真相源：谁要换算单位，都从这里走。

为什么单独一个模块
------------------
单位换算原来只存在于规格书识别（`datasheet.py`）里，于是同一份数据换个入口
进来就是另一个量级：批量导入的「单位」列压根没被读过（模板里还写着「填错不会
改变换算」），`/api/v1` 的 ingest 也没有单位字段，手工编辑页只能按字典单位填。
一次 `mL/min` 写成 `L/min` 的导入，库里的流量就差了 1000 倍。

设计上的三条硬规则
------------------
1. **只做同维度的换算**。流量换流量、压力换压力；跨维度一律拒绝并说清原因，
   绝不「取个近似」。识别时把 kPa 当 L/min 落库，比不落库糟糕得多。
2. **只做定义性的换算**。Vp → Vpp 是乘 2，这是定义，永远成立；Vrms → Vpp 取决于
   波形（正弦 2√2、方波 2），不是定义，所以不自动做，交人判断。
3. **有歧义就标出来，不猜**。原文只写「20 V」时不替人决定是峰值还是峰峰值，
   原样保留并标 `needs_review`，让导入报告把它列出来。

温度刻意不含在内：℃ ↔ ℉ 需要偏移量（不是纯因子），换算写成乘法容易出
「32 ℉ = 32 ℃」这种错。真要用的时候单独加，并且必须带测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── 维度与因子 ─────────────────────────────────────────────────────────────
# 单位写法（小写）→ (维度, 换算到该维度基准单位的因子)
# 基准：流量 L/min、压力 kPa、电压 Vpp、功率 mW、频率 kHz、长度 mm、质量 g、
#      时间 h、声压 dB(A)、占比 %。
_UNITS: dict[str, tuple[str, float]] = {
    # 流量
    "l/min": ("flow", 1.0),
    "l / min": ("flow", 1.0),
    "lpm": ("flow", 1.0),
    "ml/min": ("flow", 0.001),
    "ml / min": ("flow", 0.001),
    "µl/min": ("flow", 1e-6),
    "μl/min": ("flow", 1e-6),
    "ul/min": ("flow", 1e-6),
    "ml/s": ("flow", 0.06),
    "l/h": ("flow", 1 / 60),
    "m3/h": ("flow", 1000 / 60),
    # sccm 是「标况 mL/min」，与 mL/min 差一个温压修正；微流量场景按 1:1000 取，
    # 这个近似写在返回值里，免得有人以为它是精确定义。
    "sccm": ("flow", 0.001),
    "slm": ("flow", 1.0),
    # 压力
    "kpa": ("pressure", 1.0),
    "pa": ("pressure", 0.001),
    "mpa": ("pressure", 1000.0),
    "mbar": ("pressure", 0.1),
    "bar": ("pressure", 100.0),
    "psi": ("pressure", 6.894757),
    "mmh2o": ("pressure", 0.00980665),
    "mm h2o": ("pressure", 0.00980665),
    # 电压（只用在同一口径内；跨口径见 normalize_voltage_text）
    "vpp": ("voltage", 1.0),
    "vp-p": ("voltage", 1.0),
    "vp": ("voltage", 2.0),
    "mvpp": ("voltage", 0.001),
    "mvrms": ("voltage", 0.001),
    # 功率
    "mw": ("power", 1.0),
    "w": ("power", 1000.0),
    # 频率
    "khz": ("frequency", 1.0),
    "hz": ("frequency", 0.001),
    "mhz": ("frequency", 1000.0),
    # 长度
    "mm": ("length", 1.0),
    "cm": ("length", 10.0),
    "um": ("length", 0.001),
    "µm": ("length", 0.001),
    # 质量
    "g": ("mass", 1.0),
    "mg": ("mass", 0.001),
    "kg": ("mass", 1000.0),
    # 时间
    "h": ("time", 1.0),
    "hrs": ("time", 1.0),
    "hr": ("time", 1.0),
    "hours": ("time", 1.0),
    "小时": ("time", 1.0),
    # 声压
    "db(a)": ("sound", 1.0),
    "db (a)": ("sound", 1.0),
    "dba": ("sound", 1.0),
    "db": ("sound", 1.0),
    # 占比
    "%": ("percent", 1.0),
}

BASE_UNIT: dict[str, str] = {
    "flow": "L/min",
    "pressure": "kPa",
    "voltage": "Vpp",
    "power": "mW",
    "frequency": "kHz",
    "length": "mm",
    "mass": "g",
    "time": "h",
    "sound": "dB(A)",
    "percent": "%",
}

# 近似换算的坦白说明：值还是照换，但不假装它是精确的。
_APPROX: dict[tuple[str, str], str] = {
    ("sccm", "flow"): "sccm 按标况 mL/min 计（0.001 L/min），未做温压修正",
    ("slm", "flow"): "slm 按标况 L/min 计，未做温压修正",
}


@dataclass
class Converted:
    value: float
    ok: bool
    converted: bool  # 是否真的乘了系数（源单位 == 目标单位时为 False）
    reason: str | None = None  # ok=False 时说明为什么不能换算
    note: str | None = None  # ok=True 但值得提醒的（近似、别名等）


def unit_dimension(unit: str) -> str | None:
    """返回单位所属维度；不认识返回 None。"""
    return _UNITS.get((unit or "").strip().lower(), (None, 0.0))[0]


def same_dimension(a: str, b: str) -> bool:
    da, db = unit_dimension(a), unit_dimension(b)
    return da is not None and da == db


def convert(value: float, src_unit: str, dst_unit: str) -> Converted:
    """把 value 从 src_unit 换算成 dst_unit。

    同单位直接返回（converted=False）；跨维度或未知单位返回 ok=False 并说明原因，
    调用方必须把原因报出来 —— 静默按原值落库就是「差 1000 倍」的来源。
    """
    s = (src_unit or "").strip().lower()
    d = (dst_unit or "").strip().lower()
    if not s or s == d:
        return Converted(value=value, ok=True, converted=False)
    if s not in _UNITS:
        return Converted(value, ok=False, converted=False,
                         reason=f"不认识单位「{src_unit}」，不知道该怎么换算成 {dst_unit}")
    if d not in _UNITS:
        return Converted(value, ok=False, converted=False,
                         reason=f"指标字典里的单位「{dst_unit}」不在换算表里")
    ds, fs = _UNITS[s]
    dd, fd = _UNITS[d]
    if ds != dd:
        return Converted(
            value, ok=False, converted=False,
            reason=f"「{src_unit}」是{_CN_DIM[ds]}，「{dst_unit}」是{_CN_DIM[dd]}，跨维度不能换算",
        )
    # 12 位有效数字：0.0035 L/min → 3500 µL/min 不带上 1e-13 的浮点尾巴
    out = float(f"{value * fs / fd:.12g}")
    return Converted(out, ok=True, converted=True, note=_APPROX.get((s, ds)))


_CN_DIM = {
    "flow": "流量", "pressure": "压力", "voltage": "电压", "power": "功率",
    "frequency": "频率", "length": "长度", "mass": "质量", "time": "时间",
    "sound": "声压", "percent": "占比",
}


# ── 电压文本归一 ───────────────────────────────────────────────────────────
# 驱动电压在字典里是文本型（「11.5~21」「50Vpp (±25V Square wave)」都能放），
# 所以它不是走 convert() 的数值通道，而是把「数值 + 口径」这段文本改写规范。
#
# 口径分三组，混在一起是错的：
#   · 交流峰值：Vp、Vpp、±25V —— Vp→Vpp 乘 2 是定义，可以换算
#   · 交流有效值：Vrms —— 取决于波形（正弦 2√2、方波 2），不猜
#   · 直流：Vdc —— 乘 2 变成「峰峰值」没有物理意义，原样保留

_VOLT_RE = re.compile(
    r"(?P<sign>[±+\-])?\s*(?P<v1>\d+(?:[.,]\d+)?)\s*"
    r"(?:[~～\-–—至]\s*(?P<v2>\d+(?:[.,]\d+)?))?\s*"
    r"(?P<unit>vpp|vp-p|vp\b|vdc|dc|vrms|rms|v|mvpp|vp|mv)?",
    re.IGNORECASE,
)

# 文本里出现这些词，说明这台泵是直流驱动的，口径就是 Vdc
_DC_HINT = re.compile(r"\b(vdc|dc)\b", re.IGNORECASE)


@dataclass
class VoltageText:
    text: str  # 归一后的文本；没改动时等于原文
    converted: bool = False
    needs_review: bool = False
    note: str | None = None


def _fmt(v: float) -> str:
    """0.700 → 0.7，250.0 → 250，25.000000000000004 → 25。"""
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def normalize_voltage_text(raw: str) -> VoltageText:
    """把驱动电压的原文改写成带明确口径的写法。

    「50Vpp」→「50 Vpp」；「±25V」→「50 Vpp」；「11.5~21 Vp」→「23~42 Vpp」；
    「11.5~21 Vdc」→「11.5~21 Vdc」（直流不动）；
    「20 V」「20」→ 原样保留 + needs_review（没说是峰值还是峰峰值）。
    """
    text = (raw or "").strip()
    if not text:
        return VoltageText(text=text)
    m = _VOLT_RE.search(text)
    if not m or not m.group("v1"):
        return VoltageText(text=text)

    unit = (m.group("unit") or "").strip().lower()
    sign = m.group("sign") or ""
    v1 = float(m.group("v1").replace(",", "."))
    v2 = float(m.group("v2").replace(",", ".")) if m.group("v2") else None

    # 直流：原文写 Vdc，或文本里出现 Vdc/DC 字样 —— 乘 2 没有意义，只补口径后缀
    if unit in ("vdc", "dc") or (not unit and _DC_HINT.search(text)):
        tail = "" if unit in ("vdc", "dc") else " Vdc"
        return VoltageText(text=_with_suffix(text, tail), note="直流驱动，按 Vdc 原样保留")

    if unit in ("vrms", "rms"):
        return VoltageText(
            text=text, needs_review=True,
            note="原文是 Vrms，换算到 Vpp 取决于波形（正弦 2√2 / 方波 2），请人工确认",
        )

    peak_to_peak = unit in ("vpp", "vp-p", "mvpp")
    peak = unit in ("vp",) or (sign in ("±", "+", "-") and not peak_to_peak)
    bare = not unit or unit == "v"

    if peak_to_peak:
        factor, label = 1.0, "Vpp"
        if unit == "mvpp":
            factor = 0.001
    elif peak:
        factor, label = 2.0, "Vpp"
    elif bare:
        note = (
            "原文只写了「V」，没说是峰值还是峰峰值，未换算，请人工确认口径"
            if unit == "v"
            else "原文没写单位，无法判断是 Vp / Vpp / Vdc，未换算，请人工确认口径"
        )
        return VoltageText(text=text, needs_review=True, note=note)
    else:  # pragma: no cover - 上面的分支已覆盖所有 unit 取值
        return VoltageText(text=text)

    def scaled(v: float) -> float:
        if unit == "mvpp":
            return v * 0.001
        return v * factor

    if v2 is not None:
        body = f"{_fmt(scaled(v1))}~{_fmt(scaled(v2))}"
    else:
        body = _fmt(scaled(v1))

    # 原文里的括注是测试条件（如「±25V Square wave」），保留在文本后面
    rest = text[m.end():].strip()
    tail = f" {label}"
    if rest:
        tail += f"（{rest.strip('()（） ')}）" if rest.startswith(("(", "（")) else f" {rest}"
    return VoltageText(text=body + tail, converted=True)


def _with_suffix(text: str, suffix: str) -> str:
    if not suffix or suffix.strip().lower() in text.lower():
        return text
    return f"{text.rstrip() }{suffix}"


# 指标字典里的单位是不是电压口径 —— 决定某个文本指标要不要走 normalize_voltage_text。
# 裸 V / mV 不进 _UNITS（它们没有确定的口径，convert() 遇到就该拒绝），
# 但作为「这个指标是电压」的判断依据要算进来。
_VOLTAGE_WRITINGS = {"v", "mv", "kv", "vpp", "vp", "vp-p", "mvpp", "mvrms", "vrms", "vdc", "dc"}


def is_voltage_unit(unit: str) -> bool:
    u = (unit or "").strip().lower()
    return u in _VOLTAGE_WRITINGS or unit_dimension(u) == "voltage"
