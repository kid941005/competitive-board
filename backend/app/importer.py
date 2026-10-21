"""表格导入（Excel / CSV）：**宽表**，一行一个型号。

模板长这样（列头一字不改，与业务方在用的「友商参数登记表」一致）：

    企业名称 | 产品型号 | 产品类别 | 流量L/min | 静压Pa | 谐振频率 | 驱动电压 | 功耗 |
    噪音 | 外形尺寸 | 重量 | 温度范围 | 寿命 | 来源链接 | 置信度 |
    关联客户（有公开证据） | 口径与备注 | ……（液泵/风扇补充列）

为什么从长表改成宽表：业务方实际登记就是「一行一个型号、指标横向铺开」，长表
（一行一个指标）对人不友好 —— 录一个型号要来回写十几行。宽表可以直接对着规格书
一列列填。代价是实现上要按列头找指标、按列头单位换算，这些都收在 WIDE_COLUMNS 里。

四条硬规则（与整体设计一致）：
1. **列头带单位就换算**（「静压Pa」的 50,000 要落成 50 kPa），换算走 app/units.py。
2. **「待补充」等于未公开**，跳过不写，不算错误 —— 这是表里最高频的值。
3. **跨维度单位报错而不是取近似**（把 kPa 填进流量列）。
4. **指标必须适用于该型号大类**，否则跳过该格并说明 —— 否则会写进一条永远不显示的参数。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlmodel import Session, select

from . import adoptions, config, units
from .compare import metric_applies
from .models import Entity, Product, ProductSpec, SpecMetric, SpecSource
from .numeric import clamp_confidence, parse_number
from .sanitize import safe_url


def _fmt(value: float) -> str:
    """报告里的数字：1.1 而不是 1.1000000000000001。"""
    s = f"{value:.6f}".rstrip("0").rstrip(".")
    return s or "0"


# ── 宽表列定义 ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class WideColumn:
    header: str            # 表头原文（含单位后缀，如「静压Pa」）
    kind: str              # meta（元信息列）/ metric（指标列）
    field: str = ""        # meta 列的标识：entity / model / category / source / confidence / customers / note
    metric_key: str = ""
    unit: str = ""         # 表头声明的单位，换算用；空 = 按指标字典的单位理解
    quantity: str = ""     # 按「物理量」映射指标：同一个流量列，液泵落到 µL/min 那一项
    width: int = 14


WIDE_COLUMNS: tuple[WideColumn, ...] = (
    WideColumn("企业名称", "meta", "entity", width=22),
    WideColumn("产品型号", "meta", "model", width=18),
    WideColumn("产品类别", "meta", "category", width=12),
    # 流量与静压按「物理量」映射：同一列在液泵行落到 液体流量(µL/min) / 背压(mbar)，
    # 否则用户按他那张表填的液泵数据（0.0035 L/min、120000 Pa）会被判成「不适用」。
    WideColumn("流量L/min", "metric", metric_key="flow_air", unit="L/min",
               quantity="flow", width=12),
    WideColumn("静压Pa", "metric", metric_key="pressure_air", unit="Pa",
               quantity="pressure", width=12),
    WideColumn("谐振频率", "metric", metric_key="resonance", width=16),
    WideColumn("驱动电压", "metric", metric_key="drive_voltage", width=26),
    WideColumn("功耗", "metric", metric_key="power_mw", width=10),
    WideColumn("噪音", "metric", metric_key="noise_dba", width=8),
    WideColumn("外形尺寸", "metric", metric_key="size", width=24),
    WideColumn("重量", "metric", metric_key="weight_g", width=8),
    WideColumn("温度范围", "metric", metric_key="op_temp_max", width=12),
    WideColumn("寿命", "metric", metric_key="lifetime_h", width=8),
    WideColumn("来源链接", "meta", "source", width=46),
    WideColumn("置信度", "meta", "confidence", width=8),
    WideColumn("关联客户（有公开证据）", "meta", "customers", width=40),
    WideColumn("口径与备注", "meta", "note", width=60),
    # —— 以下 5 列是原模板没有的：不加就录不进液泵与风扇专属指标 ——
    # （液体流量/背压不单列：已有的「流量L/min」「静压Pa」会按大类自动落到它们）
    WideColumn("自吸", "metric", metric_key="self_priming", width=8),
    WideColumn("可泵介质", "metric", metric_key="media", width=14),
    WideColumn("接触介质材质", "metric", metric_key="wetted_material", width=16),
    WideColumn("厚度mm", "metric", metric_key="thickness", unit="mm", width=10),
    WideColumn("散热能力W", "metric", metric_key="heat_w", unit="W", width=12),
)

WIDE_HEADERS: list[str] = [c.header for c in WIDE_COLUMNS]

# 同一个「流量」列在不同大类下对应不同指标：液泵的量级小 10⁴，字典里单列一项
# （µL/min 与 mbar），否则对比页上 0.0035 与 0.7 混在一列里读不出量级感。
QUANTITY_METRIC: dict[str, dict[str, str]] = {
    "flow": {"pump_liquid": "flow_liquid", "*": "flow_air"},
    "pressure": {"pump_liquid": "pressure_liquid", "*": "pressure_air"},
}


def metric_for_column(col: WideColumn, metrics: dict, category: str | None):
    """某一列在某个大类下对应哪个指标。"""
    if col.quantity:
        table = QUANTITY_METRIC[col.quantity]
        return metrics.get(table.get(category or "", table["*"]))
    return metrics.get(col.metric_key)
REQUIRED_META = ("entity", "model")

# 表头别名：同一列可能被写成别的说法（业务方手改、或旧文件）
_HEADER_ALIASES: dict[str, str] = {
    "企业名称": "企业名称", "公司": "企业名称", "友商": "企业名称", "厂商": "企业名称",
    "产品型号": "产品型号", "型号": "产品型号",
    "产品类别": "产品类别", "大类": "产品类别", "类别": "产品类别",
    "来源链接": "来源链接", "来源": "来源链接", "链接": "来源链接",
    "置信度": "置信度", "可信度": "置信度",
    "关联客户（有公开证据）": "关联客户（有公开证据）", "关联客户": "关联客户（有公开证据）",
    "口径与备注": "口径与备注", "备注": "口径与备注",
}

# 「未公开」的各种写法。表里最高频的就是「待补充」，它不该算错误。
MISSING_TOKENS = {
    "", "待补充", "待定", "待确认", "无", "暂无", "未知", "未公开", "未提供",
    "—", "-", "–", "/", "na", "n/a", "none", "null",
}

# 单元格里的数字：可能是 0.155、50,000、1,420、-3
_NUM = r"[-+]?\d+(?:[.,]\d+)?"
_RE_RANGE = re.compile(rf"(?P<a>{_NUM})\s*[~～\-–—至]\s*(?P<b>{_NUM})")
_RE_NUM = re.compile(_NUM)
# 数字后面紧跟的单位写法（用来在「50,000 Pa」这类单元格里认出单位）。
# 必须以字母 / µ / ℃ / % 开头：否则「1.5 (典型)」里的括号会被当成单位。
_RE_UNIT_TAIL = re.compile(r"[A-Za-zµμ℃°%][A-Za-zµμ℃°%/³²]*")


@dataclass
class ImportReport:
    rows_read: int = 0
    created_products: int = 0
    created_entities: int = 0
    written_specs: int = 0
    skipped_missing: int = 0
    # 值、条件都没变：不写库、不记日志，也不能把人工复核标记清掉
    skipped_unchanged: int = 0
    # 真的做了单位换算的格数（mL/min → L/min、Pa → kPa、Vp → Vpp 这类）
    converted_units: int = 0
    conversions: list[str] = field(default_factory=list)
    # 单位有歧义、原样保留待人工确认的（Vrms、只写 V 的电压）
    needs_review: list[str] = field(default_factory=list)
    # 「关联客户」列落成的采用记录
    adoptions_added: list[str] = field(default_factory=list)
    # 关联客户里没法结构化、只写进备注的原文
    adoptions_skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # 条数封顶：错在「表头/单位」这种系统性问题上时，几万行会刷出几万条同样的报错，
    # 响应体膨胀到 MB 级、界面也渲染不动。只留前 MAX_ERRORS 条 + 一个被截断的计数。
    errors_truncated: int = 0
    # 逐项变更明细（供审计日志使用）：{model, metric, old, new}
    details: list[dict] = field(default_factory=list)

    MAX_ERRORS = 200

    def add_error(self, message: str) -> None:
        """记录一条错误；超过上限只累加计数。所有报错都必须走这里。"""
        if len(self.errors) < self.MAX_ERRORS:
            self.errors.append(message)
        else:
            self.errors_truncated += 1

    def as_dict(self) -> dict:
        return {
            "rows_read": self.rows_read,
            "created_products": self.created_products,
            "created_entities": self.created_entities,
            "written_specs": self.written_specs,
            "skipped_missing": self.skipped_missing,
            "skipped_unchanged": self.skipped_unchanged,
            "converted_units": self.converted_units,
            "conversions": self.conversions,
            "needs_review": self.needs_review,
            "adoptions_added": self.adoptions_added,
            "adoptions_skipped": self.adoptions_skipped,
            "errors": self.errors,
            "errors_truncated": self.errors_truncated,
        }


def _norm_key(s: str) -> str:
    return re.sub(r"[\s\-_.·/\\()（）]+", "", (s or "").strip()).lower()


def _scope_message(where: str, metric: SpecMetric, category: str) -> str:
    return (
        f"{where}：指标「{metric.name}」不适用于"
        f"「{config.category_label(category)}」，已跳过"
    )


# ── 表头识别 ───────────────────────────────────────────────────────────────
_COLUMN_BY_HEADER: dict[str, WideColumn] = {_norm_key(c.header): c for c in WIDE_COLUMNS}
for _alias, _canonical in _HEADER_ALIASES.items():
    _COLUMN_BY_HEADER.setdefault(_norm_key(_alias), _COLUMN_BY_HEADER[_norm_key(_canonical)])


def match_column(raw: str) -> WideColumn | None:
    """把表头单元格匹配到宽表列；认不出返回 None（多余的列直接忽略）。"""
    return _COLUMN_BY_HEADER.get(_norm_key(raw))


def _map_header(cells: list[str]) -> tuple[dict[int, WideColumn], bool]:
    mapping: dict[int, WideColumn] = {}
    for i, cell in enumerate(cells):
        col = match_column(cell)
        if col is not None:
            mapping[i] = col
    fields = {c.field for c in mapping.values()}
    ok = {"entity", "model"} <= fields
    return mapping, ok


def rows_from_csv(text: str) -> tuple[list[dict], list[str]]:
    """解析 CSV 文本，返回 (行字典列表, 已识别的表头)。

    每行带 `_lineno`（文件里的真实行号），报错时指得准。
    """
    reader = csv.reader(io.StringIO(text))
    mapping: dict[int, WideColumn] = {}
    headers: list[str] = []
    rows: list[dict] = []
    for lineno, raw in enumerate(reader, start=1):
        cells = [(c or "").strip() for c in raw]
        if not any(cells):
            continue
        if not mapping:
            mapping, ok = _map_header(cells)
            if ok:
                headers = [c.header for c in mapping.values()]
            else:
                mapping = {}
            continue
        row: dict[str, str] = {}
        for i, cell in enumerate(cells):
            col = mapping.get(i)
            if col is not None:
                row[col.header] = cell
        row["_lineno"] = str(lineno)
        rows.append(row)
    return rows, headers


def rows_from_xlsx(data: bytes) -> tuple[list[dict], list[str]]:
    """解析 xlsx 第一个工作表，返回 (行字典列表, 已识别的表头)。

    表头不必在第 1 行：向下找第一行同时含「企业名称」与「产品型号」的行，
    兼容带标题行的手工表。多余的辅助工作表（客户对照、官网索引…）不会被读。
    """
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        mapping: dict[int, WideColumn] = {}
        headers: list[str] = []
        rows: list[dict] = []
        for lineno, values in enumerate(ws.iter_rows(values_only=True), start=1):
            cells = ["" if v is None else str(v).strip() for v in values]
            if not any(cells):
                continue
            if not mapping:
                mapping, ok = _map_header(cells)
                if ok:
                    headers = [c.header for c in mapping.values()]
                else:
                    mapping = {}
                continue
            row: dict[str, str] = {}
            for i, cell in enumerate(cells):
                col = mapping.get(i)
                if col is not None and cell:
                    row[col.header] = cell
            if row:
                row["_lineno"] = str(lineno)
                rows.append(row)
        return rows, headers
    finally:
        wb.close()


# ── 单元格解析 ─────────────────────────────────────────────────────────────
@dataclass
class Cell:
    """一个指标格的解析结果。"""

    missing: bool = False
    value_num: float | None = None
    value_text: str | None = None
    condition: str | None = None
    converted: bool = False
    note: str | None = None
    review: str | None = None
    error: str | None = None


def _unit_token(text: str) -> str:
    m = _RE_UNIT_TAIL.search(text)
    return (m.group(0) if m else "").strip()


def parse_cell(raw: str, metric: SpecMetric, column_unit: str = "") -> Cell:
    """解析一个指标格。

    - 「待补充」/空 → missing，不算错误
    - 文本型指标 → 原样保留（电压走 units.normalize_voltage_text 统一口径）
    - 数值型指标 → 取数字（区间取上限）+ 换算到字典单位；换算不了就报错，不猜
    """
    text = (raw or "").strip()
    if text.lower() in MISSING_TOKENS:
        return Cell(missing=True)

    if metric.value_type != "number":
        if units.is_voltage_unit(metric.unit):
            vt = units.normalize_voltage_text(text)
            return Cell(value_text=vt.text, converted=vt.converted,
                        note=vt.note if vt.converted else None,
                        review=vt.note if vt.needs_review else None)
        return Cell(value_text=text)

    # 数值型：区间（0~45 ℃）取上限，原文进测试条件
    condition = None
    rng = _RE_RANGE.search(text)
    if rng:
        a = parse_number(rng.group("a"))
        b = parse_number(rng.group("b"))
        if a is None or b is None:
            return Cell(error=f"「{text}」区间里的数字认不出来")
        num = b if b >= a else a
        condition = f"原文 {rng.group('a')}~{rng.group('b')}"
    else:
        numbers = _RE_NUM.findall(text)
        found = _RE_NUM.search(text)
        num = parse_number(found.group(0)) if found else None
        if num is None:
            return Cell(error=f"「{text}」里没有可识别的数字")
        if len(numbers) > 1:
            # 「100000（水）／15000（空气）」这类并列口径：只能取一个，
            # 但必须标出来 —— 静默丢掉另一个值正是最坏的一种错。
            return Cell(
                value_num=num, review=f"单元格里有 {len(numbers)} 个数字（{text}），"
                                      f"只取了第一个，请人工确认口径",
            )

    # 单位优先级：单元格自带的 > 列头声明的 > 指标字典的。
    # 单元格写了单位但认不出来（如把「3.3 Vrms」填进静压列）必须报错 ——
    # 退回列头单位会把它当成 3.3 Pa 静默落库，是「差 1000 倍」那类错的同一族。
    token = _unit_token(text)
    if token and units.unit_dimension(token) is None:
        return Cell(error=f"单位「{token}」认不出，没法换算成 {metric.unit}")
    src_unit = token or column_unit or metric.unit
    conv = units.convert(num, src_unit, metric.unit)
    if not conv.ok:
        return Cell(error=f"「{text}」{conv.reason}")
    if conv.converted:
        condition = condition or None
        return Cell(value_num=conv.value, condition=condition, converted=True,
                    note=f"{text} → {_fmt(conv.value)} {metric.unit}" + (f"（{conv.note}）" if conv.note else ""))
    return Cell(value_num=conv.value, condition=condition)


# ── 导入 ───────────────────────────────────────────────────────────────────
def _source_cache(session: Session, batch_note: str) -> dict[str, SpecSource]:
    """同一批里相同链接只建一条来源，避免几十行生成几十条一模一样的出处。

    库里已有完全相同的一条（kind/title/url/note 都一致，即同一个文件再导一次）
    就复用它，不再新插一行。
    """
    cache: dict[str, SpecSource] = {}

    def get(url: str) -> SpecSource:
        key = safe_url(url) or ""
        if key in cache:
            return cache[key]
        src = session.exec(
            select(SpecSource).where(
                SpecSource.kind == "manual",
                SpecSource.title == "表格导入",
                SpecSource.url == (key or None),
                SpecSource.note == (batch_note or None),
            )
        ).first()
        if src is None:
            src = SpecSource(
                kind="manual",
                url=key or None,
                title="表格导入",
                captured_at=datetime.now(timezone.utc),
                note=batch_note or None,
            )
            session.add(src)
            session.flush()
        cache[key] = src
        return src

    return get


def _confidence(raw: str) -> float:
    """置信度缺省 1.0，且必须钳在 0~1。

    这里不能用 `x or 1.0`：0.0 是「完全不可信」的合法取值，
    用 or 会把最差的置信度翻成最高。
    """
    return clamp_confidence(parse_number(raw))


def resolve_category(raw: str) -> str | None:
    """把「压电气泵 / pump_air / air pump」这类写法归一成代码；不认识返回 None。"""
    s = (raw or "").strip()
    if not s:
        return None
    if s in config.CATEGORY_LABEL:
        return s
    low = s.lower().replace(" ", "").replace("_", "")
    for code, label in config.CATEGORY_LABEL.items():
        if low == label.lower() or low == code.replace("_", ""):
            return code
    short = {
        "气泵": "pump_air", "空气泵": "pump_air", "airpump": "pump_air",
        "风扇": "fan", "散热风扇": "fan",
        "液泵": "pump_liquid", "水泵": "pump_liquid", "liquidpump": "pump_liquid",
    }
    return short.get(low)


def import_rows(
    session: Session,
    rows: list[dict],
    headers: list[str],
    *,
    batch_note: str = "",
    commit: bool = True,
) -> ImportReport:
    """按宽表写入。一行 = 一个型号；行内每个指标列一格。"""
    report = ImportReport(rows_read=len(rows))
    # 表头校验：文件导入带的是识别后的规范表头，JSON 调用方（/api/v1/import）只给行，
    # 这里统一按「行里出现过的键」补一次 —— 两条通道必须给出同一句人话，
    # 否则 AI 传旧格式只会收到 41 条「产品型号为空」，看不出是自己格式错了。
    present = set(headers)
    if not present:
        for row in rows:
            for key in row:
                if str(key).startswith("_"):
                    continue
                col = match_column(str(key))
                present.add(col.header if col else str(key).strip())
    if not {"企业名称", "产品型号"} <= present:
        report.add_error(
            "没认到表头。模板已改为宽表：表头必须同时包含「企业名称」与「产品型号」，"
            "并包含各指标列（如「流量L/min」「静压Pa」）。请重新下载模板。"
        )
        return report

    metrics = {m.key: m for m in session.exec(select(SpecMetric)).all()}
    # 列都过一遍：字典里没有的指标列，只有在**真有值**时才报错（报一次），
    # 否则空列也会刷一屏噪音 —— 模板 24 列，字典少一个指标就报一次是无意义的。
    columns = [c for c in WIDE_COLUMNS if c.kind == "metric"]
    missing_reported: set[str] = set()

    entity_cache: dict[str, Entity] = {
        _norm_key(e.name): e for e in session.exec(select(Entity)).all()
    }
    # 键带 entity_id：两家友商都有同名型号时，只按型号做键会把值写到别人名下
    product_cache: dict[tuple[int, str], Product] = {
        (p.entity_id, p.model.strip().lower()): p
        for p in session.exec(select(Product)).all()
    }
    source_for = _source_cache(session, batch_note)
    spec_cache: dict[int, dict[int, ProductSpec]] = {}

    def specs_of(product_id: int) -> dict[int, ProductSpec]:
        bucket = spec_cache.get(product_id)
        if bucket is None:
            bucket = {
                s.metric_id: s
                for s in session.exec(
                    select(ProductSpec).where(ProductSpec.product_id == product_id)
                ).all()
            }
            spec_cache[product_id] = bucket
        return bucket

    for idx, row in enumerate(rows, start=2):
        normalized: dict[str, str] = {}
        for k, v in row.items():
            key = str(k).strip()
            col = match_column(key)
            normalized[col.header if col else key] = "" if v is None else str(v).strip()
        row = normalized
        raw_lineno = row.pop("_lineno", "")
        lineno = int(raw_lineno) if raw_lineno.isdigit() else idx
        where = f"第 {lineno} 行"

        model = row.get("产品型号", "")
        ent_name = row.get("企业名称", "") or "未分组"
        if not model:
            report.add_error(f"{where}：产品型号为空，整行跳过")
            continue

        category_raw = row.get("产品类别", "")
        category = None
        if category_raw and category_raw.lower() not in MISSING_TOKENS:
            category = resolve_category(category_raw)
            if category is None:
                options = "、".join(config.CATEGORY_LABEL.values())
                report.add_error(
                    f"{where}：产品类别「{category_raw}」不可识别，整行跳过。可选：{options}"
                )
                continue

        # 先把这一行的指标格都解析完，再决定要不要建档。
        # 一行如果全是「跨大类 / 单位非法 / 待补充」，不该顺手建出一个空型号 ——
        # 旧长表时代就是这样防的，宽表不能因为「一行就是一个型号」就退回去。
        pending: list[tuple[WideColumn, SpecMetric, Cell, str]] = []
        errors_before = len(report.errors)
        for col in columns:
            raw = row.get(col.header, "")
            metric = metric_for_column(col, metrics, category)
            if metric is None:
                if raw.strip() and col.metric_key not in missing_reported:
                    missing_reported.add(col.metric_key)
                    report.add_error(
                        f"「{col.header}」列有值，但指标字典里没有 {col.metric_key}，该列已忽略"
                    )
                continue
            cell = parse_cell(raw, metric, col.unit)
            tag = f"{where} {model} · {metric.name}"
            if cell.missing:
                # 只数「明确写了待补充/无」的格。真空白是「这一列没用到」，
                # 宽表 24 列里每行都有十几个空格，全数进去会变成几百，纯噪音。
                if raw.strip():
                    report.skipped_missing += 1
                continue
            if cell.error:
                report.add_error(f"{tag}：{cell.error}，已跳过")
                continue
            if category and not metric_applies(metric, category):
                report.add_error(_scope_message(tag, metric, category))
                continue
            pending.append((col, metric, cell, raw))

        cust_text = row.get("关联客户（有公开证据）", "")
        note_text = row.get("口径与备注", "")
        has_customer = bool(cust_text and cust_text.lower() not in MISSING_TOKENS)
        has_note = bool(note_text and note_text.lower() not in MISSING_TOKENS)
        if not pending and not has_customer and not has_note:
            # 整行都是「待补充/空白」= 这一款确实什么都没公开，合法状态，静默跳过。
            # 只有在这一行**因为报错**才变得没东西可写时，才补一句总结。
            if len(report.errors) > errors_before:
                report.add_error(f"{where}：没有任何可写入的值，整行跳过")
            continue

        entity = entity_cache.get(_norm_key(ent_name))
        if entity is None:
            kind = "self" if ent_name.strip() == config.SELF_COMPANY_NAME else "competitor"
            entity = Entity(kind=kind, name=ent_name)
            session.add(entity)
            session.flush()
            entity_cache[_norm_key(ent_name)] = entity
            report.created_entities += 1

        product = product_cache.get((entity.id, model.strip().lower()))
        if product is None:
            product = Product(entity_id=entity.id, model=model, category=category)
            session.add(product)
            session.flush()
            product_cache[(entity.id, model.strip().lower())] = product
            report.created_products += 1
        elif category:
            if product.category and product.category != category:
                report.add_error(
                    f"{where}：型号 {model} 已属「{config.category_label(product.category)}」，"
                    f"与表中「{config.category_label(category)}」冲突，整行跳过"
                )
                continue
            if not product.category:
                product.category = category
                session.add(product)
                session.flush()

        # 只有真要写格子时才建来源；否则「只写了备注」的行会凭空多出一条
        # 「表格导入」来源记录，攒成垃圾数据。
        src_id = source_for(row.get("来源链接", "")).id if pending else None
        conf = _confidence(row.get("置信度", ""))

        for col, metric, cell, raw in pending:
            # 行里没写大类时，只能等型号解析出来再映射指标与判适用性
            if not category:
                resolved = metric_for_column(col, metrics, product.category)
                if resolved is not None and resolved.key != metric.key:
                    metric = resolved
                    cell = parse_cell(raw, metric, col.unit)
                    if cell.missing or cell.error:
                        report.add_error(
                            f"{where} {model} · {metric.name}：{cell.error or '没有可写入的值'}，已跳过"
                        )
                        continue
                if not metric_applies(metric, product.category):
                    report.add_error(_scope_message(f"{where} {model}", metric, product.category))
                    continue
            tag = f"{where} {model} · {metric.name}"
            if cell.note:
                report.converted_units += 1
                report.conversions.append(f"{tag}：{cell.note}")
            if cell.review:
                report.needs_review.append(f"{tag}：{cell.review}")

            existing = specs_of(product.id).get(metric.id)  # type: ignore[arg-type]
            # 宽表没有「测试条件」列（一行一个型号，条件常常逐指标不同，塞不进一列）。
            # 所以「表里没给条件」必须理解成「不动已有条件」，而不是「清空」——
            # 与「表单里没出现的指标不动」是同一条原则。否则导出一份再导入，
            # 全库的 @15Vpp / 水·100Hz 会被静默抹掉，而且没人会发现。
            condition = cell.condition
            if condition is None and existing is not None:
                condition = existing.condition
            if existing is not None:
                if (
                    existing.value_num == cell.value_num
                    and (existing.value_text or None) == (cell.value_text or None)
                    and (existing.condition or None) == (condition or None)
                ):
                    report.skipped_unchanged += 1
                    continue
                old_value = existing.value_num if existing.value_num is not None else existing.value_text
                existing.value_num = cell.value_num
                existing.value_text = cell.value_text
                existing.condition = condition
                existing.source_id = src_id
                existing.confidence = conf
                # 机器又写了一遍值，人工复核就不再成立
                existing.verified = False
                existing.updated_at = datetime.now(timezone.utc)
                session.add(existing)
            else:
                old_value = None
                created = ProductSpec(
                    product_id=product.id,
                    metric_id=metric.id,
                    value_num=cell.value_num,
                    value_text=cell.value_text,
                    condition=condition,
                    source_id=src_id,
                    confidence=conf,
                    verified=False,
                )
                session.add(created)
                specs_of(product.id)[metric.id] = created  # type: ignore[index]
            report.written_specs += 1
            new_value = cell.value_num if cell.value_num is not None else cell.value_text
            report.details.append({
                "model": model,
                "metric": metric.name,
                "old": None if old_value is None else str(old_value),
                "new": None if new_value is None else str(new_value),
            })

        # 关联客户（有公开证据）→ 客户采用记录
        cust_text = row.get("关联客户（有公开证据）", "")
        if cust_text and cust_text.lower() not in MISSING_TOKENS:
            added, skipped = adoptions.add_from_text(session, product, cust_text)
            report.adoptions_added.extend(f"{model} ← {name}" for name in added)
            report.adoptions_skipped.extend(f"{model}：{msg}" for msg in skipped)

        # 口径与备注 → 型号备注（只在内容变化时写）
        note = row.get("口径与备注", "")
        if note and note.lower() not in MISSING_TOKENS and (product.note or "") != note:
            product.note = note
            session.add(product)

    # 全程 flush 不 commit：一次导入要么整批可见，要么整批回滚。
    if commit:
        session.commit()
    else:
        session.rollback()
    return report


def import_csv(session: Session, text: str, *, batch_note: str = "CSV 导入") -> ImportReport:
    rows, headers = rows_from_csv(text)
    return import_rows(session, rows, headers, batch_note=batch_note)


def import_xlsx(session: Session, data: bytes, *, batch_note: str = "Excel 导入") -> ImportReport:
    rows, headers = rows_from_xlsx(data)
    if not rows and not headers:
        rep = ImportReport()
        rep.errors.append(
            "没有读到任何数据：第一个工作表里找不到「企业名称」+「产品型号」表头。"
            "模板是宽表（一行一个型号），请重新下载模板。"
        )
        return rep
    return import_rows(session, rows, headers, batch_note=batch_note)


# ── 模板 ───────────────────────────────────────────────────────────────────
TEMPLATE_EXAMPLE: list[list] = [
    ["村田制作所（Murata）", "MZB1001T02", "压电气泵", 0.70, 1420, "24–27 kHz",
     "11.5–21 Vdc", "待补充", "待补充", "20×20×1.85 mm", 1.4, "0~70 ℃", "待补充",
     "https://www.murata.com/zh-cn/products/mechatronics/fluid", 0.90,
     "待补充（无公开客户证据）", "流量型；静压@15Vpp",
     "", "", "", "", "", "", ""],
    ["村田制作所（Murata）", "MZB3004T04", "压电气泵", 0.155, 50000, "21.5–24.5 kHz",
     "8–21 Vdc", "待补充", "待补充", "21×19×3.4 mm", "待补充", "0~45 ℃", "待补充",
     "https://www.murata.com/zh-cn/products/mechatronics/fluid", 0.90,
     "华为（WATCH D2 第三方拆解确认）；欧姆龙（HeartGuide 2019 拆解）",
     "压力型（带喷嘴）；静压@19.5Vdc", "", "", "", "", "", "", ""],
]

CSV_TEMPLATE = (
    ",".join(WIDE_HEADERS)
    + "\n"
    + "\n".join(",".join("" if c is None else str(c) for c in row) for row in TEMPLATE_EXAMPLE)
    + "\n"
)

TEMPLATE_TIPS: list[tuple[str, str]] = [
    ("列", "说明"),
    ("企业名称", "必填。已存在则复用（忽略空格、标点与大小写，含「村田制作所（Murata）」这种括注）；不存在则新建公司。"),
    ("产品型号", "必填。同一家公司下唯一；已存在则更新参数，不存在则新建型号。"),
    ("产品类别", "建议填，只能填：" + "、".join(config.CATEGORY_LABEL.values()) + "。"
                "已有型号不能改大类，冲突时整行跳过。"),
    ("指标列", "一格一个指标，留空或写「待补充」表示未公开，跳过不写。"),
    ("列头单位", "列头写的单位会被换算成系统口径：流量L/min、静压Pa（50,000 会落成 50 kPa）、"
              "液体流量µL/min、背压mbar、厚度mm、散热能力W。不要自己换算，按原表填。"),
    ("数字写法", "千分位逗号可以（50,000）；区间取上限（0~45 ℃ → 45）；「待补充」= 未公开。"),
    ("文本列", "谐振频率 / 驱动电压 / 外形尺寸 / 自吸 / 可泵介质 / 接触介质材质 按原文填，"
            "驱动电压会统一成「数值 + 口径」（Vp→Vpp 乘 2，Vrms 不猜）。"),
    ("来源链接", "整行共用一条来源，越具体越好，便于逐项复核。"),
    ("置信度", "0~1，缺省 1.0。"),
    ("关联客户（有公开证据）",
     "多家用「；」分隔，如「华为（WATCH D2 拆解确认）；欧姆龙（HeartGuide 2019）」。"
     "导入时会拆成客户采用记录（状态=传闻），括注写进备注；拆不准则只留在本列。"),
    ("口径与备注", "写进型号备注，例如「静压@19.5Vdc、流量@18Vdc」「医疗用途须签约」。"),
    ("", ""),
    ("注意", "导入的参数默认标记「未复核」，需在「产品管理」里逐项确认。"),
    ("注意", "同一份文件重复导入是安全的：值与条件没变的格会被跳过，不清复核标记。"),
    ("注意", "只读第一个工作表；「客户-友商对照」「友商官网索引」等辅助表不会被导入。"),
]


def xlsx_template() -> bytes:
    """生成宽表模板：一个「参数导入」工作表 + 一页填写说明。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "参数导入"

    head_fill = PatternFill("solid", fgColor="EEF2FF")
    ws.append(WIDE_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in TEMPLATE_EXAMPLE:
        ws.append(row)

    for i, col in enumerate(WIDE_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = col.width
    ws.freeze_panes = "C2"

    tip = wb.create_sheet("填写说明")
    for row in TEMPLATE_TIPS:
        tip.append(list(row))
    for cell in tip[1]:
        cell.font = Font(bold=True)
    tip.column_dimensions["A"].width = 20
    tip.column_dimensions["B"].width = 96

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
