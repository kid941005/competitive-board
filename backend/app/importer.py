"""表格导入（Excel / CSV）—— 扩充数据库的批量通道。

列（表头必须存在「型号」「指标」「数值」三列，其余可选，顺序不限）：

    型号, 友商, 大类, 指标, 数值, 单位, 条件, 来源链接, 置信度

规则：
- 指标按 key 或中文名匹配指标字典；**匹配不到直接报错跳过，不自动新建**。
  这是刻意的：指标字典必须先想清楚，否则会退化成每个友商一套字段。
- 产品按型号匹配；不存在则连同友商一起创建。
- 友商名做归一匹配（去空白标点、忽略大小写），避免「村田」和「村田制作所」各建一份。
- 大类只接受配置里的代码或中文名；填了非法值就报错，**不猜**。
- 数值为空表示「该型号未公开此项」，写入时跳过（不覆盖已有值）。

Excel 与 CSV 走同一套校验：读取器只负责把文件变成「表头 + 行字典」，之后完全一致。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlmodel import Session, select

from . import config
from .compare import metric_applies
from .models import Entity, Product, ProductSpec, SpecMetric, SpecSource
from .numeric import clamp_confidence, parse_number
from .sanitize import safe_url

# 规范列名 → 可接受的写法（中英文都收，避免用户改表头就导入失败）
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "型号": ("型号", "产品型号", "model", "part number", "pn"),
    "友商": ("友商", "公司", "厂商", "企业", "entity", "company", "brand"),
    "大类": ("大类", "产品大类", "类别", "category"),
    "指标": ("指标", "参数", "指标名", "metric", "parameter"),
    "数值": ("数值", "值", "measure", "value"),
    "单位": ("单位", "unit", "units"),
    "条件": ("条件", "测试条件", "condition", "test condition"),
    "来源链接": ("来源链接", "来源", "链接", "url", "source", "source url"),
    "置信度": ("置信度", "可信度", "confidence"),
}

REQUIRED_COLS = ("型号", "指标", "数值")

_ALIAS_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in COLUMN_ALIASES.items():
    for _a in _aliases:
        _ALIAS_LOOKUP[_a.strip().lower()] = _canonical


@dataclass
class ImportReport:
    rows_read: int = 0
    created_products: int = 0
    created_entities: int = 0
    written_specs: int = 0
    skipped_missing: int = 0
    # 值、条件都没变：不写库、不记日志，也不能把人工复核标记清掉
    skipped_unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    # 逐项变更明细（供审计日志使用）：{model, metric, old, new}
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rows_read": self.rows_read,
            "created_products": self.created_products,
            "created_entities": self.created_entities,
            "written_specs": self.written_specs,
            "skipped_missing": self.skipped_missing,
            "skipped_unchanged": self.skipped_unchanged,
            "errors": self.errors,
        }


def _norm_key(s: str) -> str:
    return re.sub(r"[\s\-_.·/\\()（）]+", "", (s or "").strip()).lower()


def _scope_message(lineno: int, metric: SpecMetric, category: str) -> str:
    return (
        f"第 {lineno} 行：指标「{metric.name}」不适用于"
        f"「{config.category_label(category)}」，已跳过"
    )


def normalize_header(raw: str) -> str | None:
    """把表头单元格映射成规范列名；认不出来返回 None。"""
    key = (raw or "").strip().lower().replace(" ", "").replace("_", "")
    if not key:
        return None
    for alias, canonical in _ALIAS_LOOKUP.items():
        if key == alias.replace(" ", "").replace("_", ""):
            return canonical
    return None


def _to_float(raw: str) -> float | None:
    """统一走 numeric.parse_number：逗号既可能是千分位也可能是小数点。"""
    return parse_number(raw)


def _confidence(raw: str) -> float:
    """置信度缺省 1.0，且必须钳在 0~1。

    这里不能用 `x or 1.0`：0.0 是「完全不可信」的合法取值，
    用 or 会把最差的置信度翻成最高。实测传 0 曾经落库成 1.0。
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
    # 常见别名：气泵 / 风扇 / 液泵
    short = {
        "气泵": "pump_air", "空气泵": "pump_air", "airpump": "pump_air",
        "风扇": "fan", "散热风扇": "fan",
        "液泵": "pump_liquid", "水泵": "pump_liquid", "liquidpump": "pump_liquid",
    }
    return short.get(low)


def rows_from_csv(text: str) -> tuple[list[dict], list[str]]:
    """解析 CSV 文本，返回 (行字典列表, 已识别的规范表头)。

    每行带 `_lineno`（文件里的真实行号，表头是第 1 行）。报错行号必须能对得上用户
    打开文件看到的那一行，否则「第 3 行有问题」会让人在错行上找半天。
    """
    reader = csv.reader(io.StringIO(text))
    try:
        raw_header = next(reader)
    except StopIteration:
        return [], []
    mapping = {i: normalize_header(h) for i, h in enumerate(raw_header)}
    headers = [v for v in mapping.values() if v]
    rows: list[dict] = []
    for lineno, raw in enumerate(reader, start=2):
        if not any((c or "").strip() for c in raw):
            continue
        row: dict[str, str] = {}
        for i, cell in enumerate(raw):
            canonical = mapping.get(i)
            if canonical:
                row[canonical] = (cell or "").strip()
        row["_lineno"] = str(lineno)
        rows.append(row)
    return rows, headers


def rows_from_xlsx(data: bytes) -> tuple[list[dict], list[str]]:
    """解析 xlsx 第一个工作表，返回 (行字典列表, 已识别的规范表头)。

    表头不必在第 1 行：向下找第一行含「型号」的行，兼容带标题/说明的手工表。
    每行带 `_lineno`（工作表里的真实行号），报错才指得准。
    """
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        mapping: dict[int, str] = {}
        headers: list[str] = []
        rows: list[dict] = []
        header_found = False
        for lineno, values in enumerate(ws.iter_rows(values_only=True), start=1):
            cells = ["" if v is None else str(v).strip() for v in values]
            if not any(cells):
                continue
            if not header_found:
                cand = {i: normalize_header(c) for i, c in enumerate(cells)}
                recognized = [v for v in cand.values() if v]
                # 至少认出 2 列、且含「型号」或「指标」，才算表头行。
                # 这样既能跳过标题/说明行，又不会把「缺列」的情况吞成「没读到数据」。
                if len(recognized) >= 2 and ("型号" in recognized or "指标" in recognized):
                    mapping = cand
                    headers = recognized
                    header_found = True
                continue
            row: dict[str, str] = {}
            for i, cell in enumerate(cells):
                canonical = mapping.get(i)
                if canonical and cell:
                    row[canonical] = cell
            if row:
                row["_lineno"] = str(lineno)
                rows.append(row)
        return rows, headers
    finally:
        wb.close()


def _source_cache(session: Session, batch_note: str) -> dict[str, SpecSource]:
    """同一批里相同链接只建一条来源，避免几百行生成几百条一模一样的出处。

    先按「同批 + 同链接」在内存里合并；库里已有完全相同的一条（kind/title/url/note
    都一致，即同一个文件再导一次）就复用它，不再新插一行 ——
    否则每导一次，spec_sources 就凭空长一条，且没有任何参数指向它。
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


def import_rows(
    session: Session,
    rows: list[dict],
    headers: list[str],
    *,
    batch_note: str = "",
    commit: bool = True,
) -> ImportReport:
    report = ImportReport(rows_read=len(rows))
    missing = [c for c in REQUIRED_COLS if c not in headers]
    if missing:
        report.errors.append(
            f"缺少必需列：{', '.join(missing)}（实际表头：{headers}）"
        )
        return report

    metrics = session.exec(select(SpecMetric)).all()
    by_key: dict[str, SpecMetric] = {m.key.strip().lower(): m for m in metrics}
    by_name: dict[str, list[SpecMetric]] = {}
    for m in metrics:
        by_name.setdefault(m.name.strip().lower(), []).append(m)

    def pick_metric(raw: str, category: str | None) -> tuple[SpecMetric | None, str | None]:
        """返回 (指标, 错误说明)。

        中文名可能跨大类重名（气泵与风扇都叫「静压」）。原实现用 dict 直接覆盖，
        结果同一行会被认成另一个大类的指标，名称对、口径错。
        这里按「key → 唯一名 → 用大类消歧」的顺序解析，消歧不了就明确报错。
        """
        low = raw.strip().lower()
        if low in by_key:
            return by_key[low], None
        candidates = by_name.get(low, [])
        if len(candidates) == 1:
            return candidates[0], None
        if not candidates:
            return None, f"指标「{raw}」不在指标字典中，已跳过（请先在字典里定义）"
        if category:
            scoped = [
                m for m in candidates
                if category in {s.strip() for s in (m.applies_to or "").split(",") if s.strip()}
            ]
            if len(scoped) == 1:
                return scoped[0], None
        names = "、".join(f"{m.key}" for m in candidates)
        return None, (
            f"指标名「{raw}」在多个大类下都存在（{names}），"
            "请填「大类」列消歧，或直接把指标写成 key"
        )

    entity_cache: dict[str, Entity] = {
        _norm_key(e.name): e for e in session.exec(select(Entity)).all()
    }
    # 键必须带 entity_id：两家友商都有 MP6 时，只按型号做键会让第二行
    # 拿到第一家的产品，把值写到别人名下（且数据库没有唯一约束兜底，不会报错）。
    product_cache: dict[tuple[int, str], Product] = {
        (p.entity_id, p.model.strip().lower()): p
        for p in session.exec(select(Product)).all()
    }
    source_for = _source_cache(session, batch_note)
    # 已填参数按产品懒加载：原来每行一次 select，200 行要 400 条 SQL（实测 406）。
    # 同一份表通常只有几十个型号，按产品取一次就够，查询量从 O(行) 降到 O(型号)。
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
        # 单元格统一成字符串：CSV 读出来本来就是 str，但 JSON 调用方会直接传数字/布尔/null。
        # 不归一的话 "0.7" 与 0.7 会走两条不同分支，甚至 AttributeError。
        row = {
            str(k).strip(): ("" if v is None else str(v).strip())
            for k, v in row.items()
        }
        # 读取器会带上文件里的真实行号（表头可能不在第 1 行）；JSON 调用方没有，按顺序推
        raw_lineno = row.pop("_lineno", "")
        lineno = int(raw_lineno) if raw_lineno.isdigit() else idx

        model = row.get("型号", "")
        metric_raw = row.get("指标", "")
        value_raw = row.get("数值", "")

        if not model or not metric_raw:
            report.errors.append(f"第 {lineno} 行：型号或指标为空，已跳过")
            continue

        # 大类：填了就必须认得出，认不出不猜
        category_raw = row.get("大类", "")
        category = None
        if category_raw:
            category = resolve_category(category_raw)
            if category is None:
                options = "、".join(f"{label}（{code}）" for code, label in config.CATEGORY_LABEL.items())
                report.errors.append(
                    f"第 {lineno} 行：大类「{category_raw}」不可识别，已跳过。可选：{options}"
                )
                continue

        # 指标：先按 key，再按唯一名，重名时用大类消歧
        metric, problem = pick_metric(metric_raw, category)
        if metric is None:
            report.errors.append(f"第 {lineno} 行：{problem}")
            continue

        if not value_raw:
            report.skipped_missing += 1
            continue

        # 指标适用性：与批量接口同一条规则。行里写了大类就先判断，
        # 这样被拒的行不会顺手建出一个空型号（建了也无参数，纯垃圾）。
        if category and not metric_applies(metric, category):
            report.errors.append(_scope_message(lineno, metric, category))
            continue

        # 友商 / 产品
        ent_name = row.get("友商", "") or "未分组"
        entity = entity_cache.get(_norm_key(ent_name))
        if entity is None:
            entity = Entity(kind="competitor", name=ent_name)
            session.add(entity)
            session.flush()
            entity_cache[_norm_key(ent_name)] = entity
            report.created_entities += 1

        cache_key = (entity.id, model.lower())
        product = product_cache.get(cache_key)
        if product is None:
            product = Product(entity_id=entity.id, model=model, category=category)
            session.add(product)
            session.flush()
            product_cache[cache_key] = product
            report.created_products += 1
        elif category:
            # 已有型号不能改换大类：那会让历史参数落到错误的指标体系里
            if product.category and product.category != category:
                report.errors.append(
                    f"第 {lineno} 行：型号 {model} 已属「{config.category_label(product.category)}」，"
                    f"与表中「{config.category_label(category)}」冲突，已跳过"
                )
                continue
            if not product.category:
                product.category = category
                session.add(product)
                session.flush()

        # 行里没写大类时，只能等型号解析出来再判断（已有型号可能自带分类；
        # 新建型号则分类为空，此时不做判断 —— 不能因为分类没填就拒收）。
        if not category and not metric_applies(metric, product.category):
            report.errors.append(_scope_message(lineno, metric, product.category))
            continue

        num = _to_float(value_raw)
        is_number = metric.value_type == "number"
        if is_number and num is None:
            report.errors.append(
                f"第 {lineno} 行：指标「{metric.name}」要求数值，但收到「{value_raw}」，已跳过"
            )
            continue

        existing = specs_of(product.id).get(metric.id)  # type: ignore[arg-type]
        new_num = num if is_number else None
        new_text = None if is_number else value_raw
        new_cond = row.get("条件") or None
        new_value = num if is_number else value_raw

        if existing is not None:
            # 值与条件都没变就什么都不做：不写库、不记日志、更不清人工复核标记。
            # 原来无条件覆盖，于是「把同一份 Excel 再导一遍」会把全库已复核标记清空。
            if (
                existing.value_num == new_num
                and (existing.value_text or None) == (new_text or None)
                and (existing.condition or None) == new_cond
            ):
                report.skipped_unchanged += 1
                continue

            old_value = existing.value_num if existing.value_num is not None else existing.value_text
            existing.value_num = new_num
            existing.value_text = new_text
            existing.condition = new_cond
            existing.source_id = source_for(row.get("来源链接", "")).id
            existing.confidence = _confidence(row.get("置信度", ""))
            # 机器又写了一遍值，人工复核就不再成立 —— 否则页面会一直显示「已复核」
            existing.verified = False
            existing.updated_at = datetime.now(timezone.utc)
            session.add(existing)
        else:
            old_value = None
            session.add(
                ProductSpec(
                    product_id=product.id,
                    metric_id=metric.id,
                    value_num=new_num,
                    value_text=new_text,
                    condition=new_cond,
                    source_id=source_for(row.get("来源链接", "")).id,
                    confidence=_confidence(row.get("置信度", "")),
                    verified=False,
                )
            )
        report.written_specs += 1
        report.details.append({
            "model": model,
            "metric": metric.name,
            "old": None if old_value is None else str(old_value),
            "new": None if new_value is None else str(new_value),
        })

    # 全程用 flush 而不是 commit：一次导入要么整批可见，要么整批回滚。
    # 早期版本每建一个实体就 commit，中途报错会留下半成品数据。
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
        rep.errors.append("没有读到任何数据：请确认第一个工作表里存在「型号」「指标」表头")
        return rep
    return import_rows(session, rows, headers, batch_note=batch_note)


# 模板表头与示例行（Excel / CSV 共用）
TEMPLATE_HEADER = ["型号", "友商", "大类", "指标", "数值", "单位", "条件", "来源链接", "置信度"]
TEMPLATE_ROWS = [
    ["MZB1001T02", "村田制作所", "压电气泵", "空气流量", 0.7, "L/min", "@15Vpp",
     "https://www.murata.com/zh-cn/products/mechatronics/fluid", 1.0],
    ["MZB1001T02", "村田制作所", "压电气泵", "静压", 1.42, "kPa", "@15Vpp",
     "https://www.murata.com/zh-cn/products/mechatronics/fluid", 1.0],
    ["PH-200", "深圳新锐压电", "压电风扇", "风量", 1.8, "L/min", "",
     "", 1.0],
]

CSV_TEMPLATE = (
    ",".join(TEMPLATE_HEADER)
    + "\n"
    + "\n".join(",".join(str(c) for c in row) for row in TEMPLATE_ROWS)
    + "\n"
)


def xlsx_template() -> bytes:
    """生成带表头样式与说明页的 xlsx 模板。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "参数导入"

    head_fill = PatternFill("solid", fgColor="EEF2FF")
    ws.append(TEMPLATE_HEADER)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center")
    for row in TEMPLATE_ROWS:
        ws.append(row)

    widths = [16, 18, 12, 14, 10, 10, 12, 44, 10]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    tip = wb.create_sheet("填写说明")
    lines = [
        ("列", "说明"),
        ("型号", "必填。已存在则更新该型号参数；不存在则新建型号。"),
        ("友商", "公司名称。已存在则复用（忽略空格、标点与大小写）；不存在则新建公司。"),
        ("大类", "可选，但强烈建议填。只能填：" + "、".join(config.CATEGORY_LABEL.values())),
        ("指标", "必填。填指标中文名或 key，必须在指标字典中存在，否则该行跳过。"),
        ("数值", "必填。数值型指标填数字；文本型指标填原文（如 24~27）。留空表示未公开，跳过。"),
        ("单位", "仅作说明，实际单位以指标字典为准；填错不会改变换算。"),
        ("条件", "如 @15Vpp、水·100Hz，会显示在溯源浮层。"),
        ("来源链接", "越具体越好，便于逐项复核。"),
        ("置信度", "0~1，缺省 1.0。"),
        ("", ""),
        ("注意", "导入的参数默认标记「未复核」，需在「产品管理」里逐项确认。"),
        ("注意", "型号不能改换大类：已有型号若与表中大类冲突，该行会报错跳过。"),
    ]
    for row in lines:
        tip.append(row)
    for cell in tip[1]:
        cell.font = Font(bold=True)
    tip.column_dimensions["A"].width = 16
    tip.column_dimensions["B"].width = 88

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
