"""示例数据 —— 压电气泵 / 压电风扇 / 压电液泵。

范围说明：只覆盖**器件**型号。压电陶瓷是材料牌号（d33/Qm 这类典型值表），
与器件规格书形态不同，不纳入本看板，故指标字典里也没有陶瓷那一组。

⚠️ 数据来源与可信度，请务必看清：

| 公司 | 数据来源 | confidence | verified |
|---|---|---|---|
| 村田制作所（MZB 系列） | 村田官网「微型风扇（空气泵）」产品阵容参数表 | 1.0 | 是 |
| 巴特尔斯（mp6 系列） | Bartels mp6 Micropumps Datasheet v2.1 | 0.95 | 是 |
| xMEMS / Frore Systems | 2026 固态散热横向对比文（转引厂商公开数据） | 0.75 | 否 |

「verified=否」表示数值来自公开文献或二手汇总，**投放使用前必须用厂商现行数据手册逐项复核**。
缺失项为刻意保留：厂商未公开即记为缺失，这正是看板要暴露的信息（谁在藏参数）。

已知友商中另有多家只建实体不建型号（奥迪威、攀特、嘉康、江佳、摩根），
待有公开规格书时用导入页或 CSV 补全 —— 先建实体是为了让名称归一，避免同一家建出两份档案。

替换为自有真实数据的两种方式：
  1) 改本文件的 METRICS / ENTITIES / PRODUCTS / SPECS 后重跑 `python -m app.seed --force`
  2) 服务运行中用 CSV 导入：`POST /api/import`（格式见 README）
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlmodel import Session, select

from . import config
from .db import engine, init_db
from .models import Entity, Product, ProductSpec, SpecMetric, SpecSource

# ── 指标字典 ───────────────────────────────────────────────────────────────
# (key, 指标组, 名称, 单位, 值类型, better, 精度, 排序, 关键指标, 适用大类, 说明)
#
# applies_to 为空 = 全类别通用；否则只在所选产品属于这些大类时显示该行。
# 气泵看流量/静压、风扇看风量/厚度、液泵看流量µL级/背压 —— 三套口径不通用，必须分开。
METRICS = [
    # 气动与流体
    # 流量与静压**不按大类分家**：气泵吹气、风扇推风，物理量是同一个（流量 L/min、
    # 压差 kPa），分两套指标会让「气泵 vs 风扇」的对比里出现两行流量、两行静压。
    # 液泵仍然单列 —— 它的量级差了 10⁴（µL/min 与 mbar），混在一起没法读。
    ("flow_air", "气动与流体", "空气流量", "L/min", "number", "higher", 3, 300, True, "pump_air,fan", ""),
    ("pressure_air", "气动与流体", "静压", "kPa", "number", "higher", 2, 310, True, "pump_air,fan", ""),
    ("flow_liquid", "气动与流体", "液体流量", "µL/min", "number", "higher", 0, 350, True, "pump_liquid", "典型值"),
    ("pressure_liquid", "气动与流体", "背压", "mbar", "number", "higher", 0, 360, True, "pump_liquid", "典型值"),
    ("self_priming", "气动与流体", "自吸", "", "text", "na", 0, 370, False, "pump_liquid", ""),
    ("media", "气动与流体", "可泵介质", "", "text", "na", 0, 380, False, "pump_liquid", ""),
    # 驱动与功耗
    ("resonance", "驱动与功耗", "谐振频率", "kHz", "text", "na", 0, 400, False, "pump_air,pump_liquid,fan", "厂商多以区间给出，按文本记录"),
    ("drive_voltage", "驱动与功耗", "驱动电压", "V", "text", "na", 0, 410, False, "pump_air,pump_liquid,fan", "区间值按原文记录，交直流见溯源"),
    ("power_mw", "驱动与功耗", "功耗", "mW", "number", "lower", 0, 420, True, "pump_air,pump_liquid,fan", ""),
    ("noise_dba", "驱动与功耗", "噪音", "dB(A)", "number", "lower", 1, 430, False, "pump_air,pump_liquid,fan", "气泵厂商普遍不公开此项"),
    # 物理与可靠性（器件类通用；材料牌号不在本看板范围内）
    ("size", "物理与可靠性", "外形尺寸", "mm", "text", "na", 0, 500, False, "pump_air,pump_liquid,fan", ""),
    ("thickness", "物理与可靠性", "厚度", "mm", "number", "lower", 2, 510, True, "fan", "固态散热的核心竞争力维度"),
    ("weight_g", "物理与可靠性", "重量", "g", "number", "lower", 1, 520, False, "pump_air,pump_liquid,fan", ""),
    ("heat_w", "物理与可靠性", "散热能力", "W", "number", "higher", 1, 530, False, "fan", "单颗可带走的热功耗"),
    # 工作温度天生是范围：只存上限，低温能力（-40 与 0 的差别）就全丢了。
    # 拆成两格，两端各按自己的方向比：下限越低越好、上限越高越好。
    ("op_temp_min", "物理与可靠性", "工作温度下限", "℃", "number", "lower", 0, 535, False, "pump_air,pump_liquid,fan", "越低越耐低温"),
    ("op_temp_max", "物理与可靠性", "工作温度上限", "℃", "number", "higher", 0, 540, False, "pump_air,pump_liquid,fan", "越高越耐高温"),
    ("lifetime_h", "物理与可靠性", "寿命", "h", "number", "higher", 0, 550, False, "pump_air,pump_liquid,fan", ""),
    ("wetted_material", "物理与可靠性", "接触介质材质", "", "text", "na", 0, 560, False, "pump_liquid", ""),
]

# ── 识别别名 ───────────────────────────────────────────────────────────────
# 规格书里同一指标的常见写法。自动识别时按此匹配，长别名优先。
# 注意：不要写过于泛化的词（如裸「压力」），否则会命中「压力型」这类修饰语；
# 确需保留时由 datasheet.py 的限定词规则降权。
ALIASES = {
    "flow_air": "流量, 空气流量, 气体流量, 最大流量, 风量, 最大风量, flow rate, air flow, airflow, flow",
    "pressure_air": "静压, 最大静压, 输出压力, 压力, static pressure, max pressure",
    "flow_liquid": "流量, 液体流量, flow rate, flow",
    "pressure_liquid": "背压, 压力, back pressure",
    "self_priming": "自吸, self-priming",
    "media": "可泵介质, 适用介质, 介质",
    "resonance": "谐振频率, 驱动频率, 共振频率, resonant frequency, drive frequency",
    "drive_voltage": "驱动电压, 输入电压, 电压, input voltage, drive voltage",
    "power_mw": "功耗, 功率, power consumption, power",
    "noise_dba": "噪音, 噪声, 声压级, noise",
    "size": "外形尺寸, 尺寸, 规格尺寸, dimensions, size",
    "thickness": "厚度, thickness",
    "weight_g": "重量, 质量, weight",
    "heat_w": "散热能力, 散热功率, heat dissipation",
    "op_temp_min": "工作温度下限, 最低工作温度, 低温, min operating temperature",
    "op_temp_max": "工作温度, 工作温度上限, 环境温度, operating temperature",
    "lifetime_h": "寿命, 使用寿命, lifetime, life",
    "wetted_material": "接触介质材质, 润湿材料, 材质, wetted material",
}

# ── 实体 ───────────────────────────────────────────────────────────────────
# (key, kind, 名称, 英文名, 地区, 分级)
ENTITIES = [
    # 本方公司直接用真实名称，页面上不再出现「本方 / 我方」这类代称
    ("self", "self", config.SELF_COMPANY_NAME, None, "中国", "A"),
    ("murata", "competitor", "村田制作所", "Murata", "日本", "A"),
    ("xmems", "competitor", "xMEMS Labs", "xMEMS", "美国", "B"),
    ("frore", "competitor", "Frore Systems", "Frore Systems", "美国", "B"),
    ("bartels", "competitor", "巴特尔斯微技术", "Bartels Mikrotechnik", "德国", "B"),
    # 以下只建实体、暂无型号。先建档是为了让导入时的名称归一，
    # 避免同一家因写法不同（「攀特」/「苏州攀特电陶」）建出两份档案。
    ("morgan", "competitor", "摩根先进材料", "Morgan Advanced Materials", "英国", "A"),
    ("audiowell", "competitor", "广东奥迪威", "AUDIOWELL", "中国", "B"),
    ("pant", "competitor", "苏州攀特电陶", "PANT", "中国", "B"),
    ("jiakang", "competitor", "浙江嘉康电子", "JIAKANG", "中国", "C"),
    ("jiangjia", "competitor", "江苏江佳电子", "JIANGJIA", "中国", "C"),
]

# ── 来源 ───────────────────────────────────────────────────────────────────
SOURCES = {
    "murata_fluid": dict(
        kind="webpage",
        url="https://www.murata.com/zh-cn/products/mechatronics/fluid",
        title="村田 微型风扇（空气泵）产品阵容 · 参数表",
        note="官网参数表，四型号横向对比；功耗与噪音未公开",
    ),
    "bartels_ds": dict(
        kind="datasheet",
        url="https://www.ensensetech.com/zh-tw/micropumps/mp6_series/mp6_micropumps_Datasheet_2.1V.pdf",
        title="Bartels mp6 Micropumps Datasheet v2.1",
        note="250Vpp/100Hz(SRS) 测水，250Vpp/300Hz 测气；25℃ 1bar",
    ),
    "solid_state_2026": dict(
        kind="webpage",
        url="https://yplasma.tech/blog/solid-state-cooling-landscape-2026",
        title="Solid-state cooling landscape 2026 · 厂商公开数据横向对比",
        note="二手汇总，转引厂商公开数据；Frore 未公开风量",
    ),
    "bartels_series": dict(
        kind="webpage",
        url="https://www.daleserv.com.sg/imba-testing",
        title="Bartels mp6 系列参数页（含 mp6-pi / mp6-pp 变体）",
        note="代理商转述 Bartels 数据，变体参数须以官方数据手册复核",
    ),
}

# ── 产品 ───────────────────────────────────────────────────────────────────
# (实体, 型号, 名称, 产品大类, 来源, confidence, verified)
PRODUCTS = [
    ("murata", "MZB1001T02", "MZB1001T02 微型气泵（流量型）", "pump_air", "murata_fluid", 1.0, True),
    ("murata", "MZB3004T04", "MZB3004T04 微型气泵（压力型）", "pump_air", "murata_fluid", 1.0, True),
    ("murata", "MZB3005T06", "MZB3005T06 微型气泵（薄型压力型）", "pump_air", "murata_fluid", 1.0, True),
    ("murata", "MZB4001T05", "MZB4001T05 微型气泵（压力流量型）", "pump_air", "murata_fluid", 1.0, True),
    ("bartels", "mp6-gas", "mp6-gas 压电微泵", "pump_air", "bartels_ds", 0.95, True),
    ("bartels", "mp6-gas+", "mp6-gas+ 压电微泵", "pump_air", "bartels_ds", 0.95, True),
    ("bartels", "mp6-liq", "mp6-liq 压电微泵", "pump_liquid", "bartels_ds", 0.95, True),
    ("bartels", "mp6-pi", "mp6-pi 压电微泵（PI 阀片）", "pump_liquid", "bartels_series", 0.85, False),
    ("bartels", "mp6-pp", "mp6-pp 压电微泵（PP 阀片）", "pump_liquid", "bartels_series", 0.85, False),
    ("xmems", "XMC-2400", "XMC-2400 固态微型风扇", "fan", "solid_state_2026", 0.75, False),
    ("frore", "AirJet Mini G2", "AirJet Mini G2 固态散热芯片", "fan", "solid_state_2026", 0.75, False),
]

# ── 参数 ───────────────────────────────────────────────────────────────────
# 型号 → {指标 key: 值}。文本型给字符串，数值型给数字；不写 = 该型号未公开。
SPECS: dict[str, dict[str, float | str]] = {
    # 村田 · MZB 系列
    # 村田 · MZB 系列
    "MZB1001T02": {
        "flow_air": 0.7, "pressure_air": 1.42, "resonance": "24~27",
        "drive_voltage": "11.5~21", "op_temp_max": 70,
        "size": "20×20×1.85", "weight_g": 1.4,
    },
    "MZB3004T04": {
        "flow_air": 0.155, "pressure_air": 50.0, "resonance": "21.5~24.5",
        "drive_voltage": "8~21", "op_temp_max": 45, "size": "21×21",
    },
    "MZB3005T06": {
        "flow_air": 0.155, "pressure_air": 50.0, "resonance": "21.5~24.5",
        "drive_voltage": "8~21", "op_temp_max": 45, "size": "19×19",
    },
    "MZB4001T05": {
        "flow_air": 0.5, "resonance": "20~22",
        "drive_voltage": "20~28", "op_temp_max": 50, "size": "φ28",
    },

    # Bartels · mp6 系列（气体型号已换算为 L/min 与 kPa）
    "mp6-gas": {
        "flow_air": 0.025, "pressure_air": 12.0, "drive_voltage": "250",
        "power_mw": 50, "op_temp_max": 70, "size": "30×15×3.8",
        "weight_g": 2.0, "lifetime_h": 5000,
    },
    "mp6-gas+": {
        "flow_air": 0.030, "pressure_air": 16.0, "drive_voltage": "250",
        "power_mw": 50, "op_temp_max": 70, "size": "30×15×3.8",
        "weight_g": 2.0, "lifetime_h": 5000,
    },
    "mp6-liq": {
        "flow_liquid": 8000, "pressure_liquid": 500, "drive_voltage": "250",
        "power_mw": 50, "op_temp_max": 70, "size": "30×15×3.8",
        "weight_g": 2.0, "lifetime_h": 5000,
        "self_priming": "是", "wetted_material": "PPSU", "media": "液体与气体",
    },
    "mp6-pi": {
        "flow_liquid": 6000, "pressure_liquid": 500, "drive_voltage": "250",
        "power_mw": 50, "size": "30×15×3.8", "weight_g": 2.0,
        "lifetime_h": 5000, "wetted_material": "PPSU + PI", "media": "液体与气体",
    },
    "mp6-pp": {
        "flow_liquid": 4000, "pressure_liquid": 500, "drive_voltage": "250",
        "power_mw": 50, "size": "30×15×3.8", "weight_g": 2.0,
        "lifetime_h": 5000, "wetted_material": "PP", "media": "液体与气体",
    },

    # 固态散热（风扇的静压已从 Pa 换算成 kPa，与气泵同一口径）
    "XMC-2400": {
        "flow_air": 1.5, "pressure_air": 1.3, "power_mw": 200,
        "noise_dba": 13.7, "thickness": 1.13, "drive_voltage": "20",
    },
    # Frore 未公开风量 —— 刻意留空，用于体现「未公开」在选型时的实际影响
    "AirJet Mini G2": {
        "pressure_air": 1.75, "power_mw": 1000, "noise_dba": 21.0,
        "thickness": 2.65, "heat_w": 7.5,
    },
}

# 每条参数的测试条件（写入 condition 字段，溯源浮层可见）
CONDITIONS = {
    "MZB1001T02": {"flow_air": "@15Vpp", "pressure_air": "@15Vpp", "drive_voltage": "Vdc"},
    "MZB3004T04": {"flow_air": "@18Vdc", "pressure_air": "@21Vdc", "drive_voltage": "Vdc"},
    "MZB3005T06": {"flow_air": "@18Vdc", "pressure_air": "@21Vdc", "drive_voltage": "Vdc"},
    "MZB4001T05": {"flow_air": "@24Vdc", "drive_voltage": "Vdc"},
    "mp6-gas": {"flow_air": "空气·250Vpp/300Hz", "pressure_air": "空气·250Vpp/300Hz"},
    "mp6-gas+": {"flow_air": "空气·250Vpp/300Hz", "pressure_air": "空气·250Vpp/300Hz"},
    "mp6-liq": {"flow_liquid": "水·250Vpp/100Hz", "pressure_liquid": "水·250Vpp/100Hz"},
    "mp6-pi": {"flow_liquid": "水·250Vpp/100Hz", "pressure_liquid": "水·250Vpp/100Hz"},
    "mp6-pp": {"flow_liquid": "水·250Vpp/100Hz", "pressure_liquid": "水·250Vpp/100Hz"},
    "XMC-2400": {"drive_voltage": "Vpp"},
}


def _clear(session: Session) -> None:
    for model in (ProductSpec, SpecSource, Product, SpecMetric, Entity):
        for row in session.exec(select(model)).all():  # type: ignore[arg-type]
            session.delete(row)
    session.commit()


def seed(force: bool = False, metrics_only: bool = False) -> dict[str, int]:
    """写入压电领域的内置数据。

    metrics_only=True 时只写指标字典（18 个指标，含适用大类），
    不建任何公司、型号、参数 —— 这是「空库」最常用的入口：
    看板的行来自字典，没有字典什么都渲染不出来，但用户往往不想要示例公司。
    """
    init_db()
    counts = {"metrics": 0, "entities": 0, "products": 0, "specs": 0}

    with Session(engine) as session:
        # 幂等判断按「这次要写的东西」来判，不能一律看 Entity：
        # --metrics-only 只写字典，库里没有实体本来就是正常状态。
        gate = SpecMetric if metrics_only else Entity
        existing = session.exec(select(gate)).first()
        if existing and not force:
            return {"skipped": 1}
        if force:
            _clear(session)

        # 指标字典
        metric_by_key: dict[str, SpecMetric] = {}
        for key, cat, name, unit, vtype, better, prec, order, iskey, applies, desc in METRICS:
            m = SpecMetric(
                key=key, category=cat, name=name, unit=unit, value_type=vtype,
                better=better, precision=prec, display_order=order,
                is_key_metric=iskey, applies_to=applies, description=desc or None,
                aliases=ALIASES.get(key, ""),
            )
            session.add(m)
            metric_by_key[key] = m
            counts["metrics"] += 1

        session.commit()
        for obj in metric_by_key.values():
            session.refresh(obj)

        if metrics_only:
            return counts

        # 实体
        entity_by_key: dict[str, Entity] = {}
        for key, kind, name, name_en, region, tier in ENTITIES:
            e = Entity(kind=kind, name=name, name_en=name_en, region=region, tier=tier)
            session.add(e)
            entity_by_key[key] = e
            counts["entities"] += 1

        session.commit()
        for obj in entity_by_key.values():
            session.refresh(obj)

        # 产品 + 来源 + 参数
        for ekey, model, name, cat, src_key, conf, verified in PRODUCTS:
            product = Product(
                entity_id=entity_by_key[ekey].id,  # type: ignore[arg-type]
                model=model, name=name, category=cat,
                datasheet_url=SOURCES[src_key].get("url"),
                status="active",
            )
            session.add(product)
            session.commit()
            session.refresh(product)
            counts["products"] += 1

            meta = SOURCES[src_key]
            src = SpecSource(
                kind=meta["kind"], url=meta.get("url"), title=meta.get("title"),
                captured_at=datetime.now(timezone.utc), note=meta.get("note"),
            )
            session.add(src)
            session.commit()
            session.refresh(src)

            conds = CONDITIONS.get(model, {})
            for key, value in SPECS.get(model, {}).items():
                metric = metric_by_key[key]
                is_num = isinstance(value, (int, float))
                session.add(
                    ProductSpec(
                        product_id=product.id,  # type: ignore[arg-type]
                        metric_id=metric.id,  # type: ignore[arg-type]
                        value_num=float(value) if is_num else None,
                        value_text=None if is_num else str(value),
                        condition=conds.get(key),
                        source_id=src.id,
                        confidence=conf,
                        verified=verified,
                    )
                )
                counts["specs"] += 1

        session.commit()

    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="写入压电领域内置数据")
    ap.add_argument("--force", action="store_true", help="清空现有数据后重建")
    ap.add_argument(
        "--metrics-only", action="store_true",
        help="只写指标字典（含适用大类），不建任何公司/型号 —— 空库启动后用它拿一套可用字典",
    )
    args = ap.parse_args()
    print(seed(force=args.force, metrics_only=args.metrics_only))


if __name__ == "__main__":
    main()
