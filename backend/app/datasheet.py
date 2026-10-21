"""规格书参数自动识别（PDF / 纯文本）。

设计原则与整体方案一致：**机器抽取 + 人工复核，绝不静默入库**。
抽取结果一律以 Candidate 返回，由人在 review 页确认后才写入参数表，落库时 verified=False。

四条约束：
1. 只在「该产品大类适用的指标」范围内匹配 —— 否则「流量」会在气泵与液泵之间误配。
2. 单位不一致时显式换算并标记 converted；找不到换算系数就降低置信度，不猜。
3. 抽不到就是抽不到，不用邻近数字硬凑。
4. 同一指标多处出现时保留置信度最高的一处，并记录命中次数供参考（数据手册常在
   不同测试条件下重复给同一指标，P0 的数据模型只能存一个值）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .models import SpecMetric

# 标签后向后搜索的范围（字符）。太小会漏掉「标签在表头、数值在下一行」的版式，
# 太大会把无关行抓进来。
WINDOW = 90

# 紧跟在标签后、说明这不是一个取值标签的词
_QUALIFIER_TAIL = ("型", "式", "比", "范围", "上限", "下限", "等级", "特性", "曲线")

_NUM = r"[-+]?\d+(?:[.,]\d+)?"
# 区间与比较符分开定义：_RANGE 曾单独存在但从未被引用（_RE_VALUE 里用的是命名组），
# 留着只会让人以为区间解析走的是它。
_CMP = r"(?:≥|≤|>=|<=|＞|＜|>|<|约|min\.?|max\.?)"

# 数字 + 可选区间 + 可选单位
_RE_VALUE = re.compile(
    rf"(?P<cmp>{_CMP})?\s*(?P<v1>{_NUM})\s*(?:(?:[~～\-–—至])\s*(?P<v2>{_NUM}))?",
    re.IGNORECASE,
)

# 指标标准单位 → 文档中可能出现的写法
UNIT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "L/min": ("l/min", "l / min", "lpm", "l/min.", "ml/min", "ml / min", "µl/min", "μl/min", "ul/min"),
    "µL/min": ("µl/min", "μl/min", "ul/min", "ml/min", "ml / min", "µl / min", "l/min"),
    "kPa": ("kpa", "mbar", "bar", "mpa", "pa"),
    "Pa": ("pa", "kpa"),
    "mbar": ("mbar", "kpa", "bar"),
    "dB(A)": ("db(a)", "dba", "db (a)", "db"),
    "kHz": ("khz", "hz"),
    "mW": ("mw", "w"),
    "W": ("w", "mw"),
    "V": ("vdc", "vpp", "v", "mv"),
    "mm": ("mm", "cm"),
    "g": ("g", "mg"),
    "℃": ("℃", "°c", "degc", "摄氏度"),
    "h": ("h", "hrs", "hr", "hours", "小时"),
    "%": ("%",),
    "": (),
}

# 所有已知单位写法，长的优先。用于识别「紧贴数值的却是不相干单位」——
# 例：指标为 µL/min 而原文写 0.7 kPa，不能因为「找不到本指标单位」就退而取值。
_ALL_UNIT_SYNS: tuple[str, ...] = tuple(
    sorted({syn for syns in UNIT_SYNONYMS.values() for syn in syns}, key=len, reverse=True)
)

# 找到的单位 → 该指标的标准单位，对应的换算系数。
# ⚠️ 键必须统一小写：_convert() 查找前会把两侧都 lower()，此前这里写了 "µL/min"
# 这类首字母大写的键，导致 l/min → µL/min 的换算静默失效（值原样落库，差 10⁶ 倍）。
_CONVERT_RAW: dict[tuple[str, str], float] = {
    ("ml/min", "µL/min"): 1000.0,
    ("l/min", "µL/min"): 1_000_000.0,
    ("ml/min", "l/min"): 0.001,
    ("µl/min", "l/min"): 1e-6,
    ("ul/min", "l/min"): 1e-6,
    ("pa", "kpa"): 0.001,
    ("mpa", "kpa"): 1000.0,
    ("mbar", "kpa"): 0.1,
    ("bar", "kpa"): 100.0,
    ("kpa", "pa"): 1000.0,
    ("kpa", "mbar"): 10.0,
    ("bar", "mbar"): 1000.0,
    ("mw", "w"): 0.001,
    ("w", "mw"): 1000.0,
    ("cm", "mm"): 10.0,
    ("hz", "khz"): 0.001,
    ("mv", "v"): 0.001,
    ("mg", "g"): 0.001,
}
UNIT_CONVERT: dict[tuple[str, str], float] = {
    (a.strip().lower(), b.strip().lower()): f for (a, b), f in _CONVERT_RAW.items()
}


@dataclass
class Candidate:
    metric_key: str | None
    metric_name: str | None
    unit: str
    value_num: float | None = None
    value_text: str | None = None
    matched_label: str = ""
    condition: str | None = None
    page: int = 1
    context: str = ""
    confidence: float = 0.0
    converted: bool = False
    unit_found: str | None = None
    qualifier: bool = False
    hits: int = 1

    def to_dict(self) -> dict:
        return {
            "metric_key": self.metric_key,
            "metric_name": self.metric_name,
            "unit": self.unit,
            "value_num": self.value_num,
            "value_text": self.value_text,
            "matched_label": self.matched_label,
            "condition": self.condition,
            "page": self.page,
            "context": self.context,
            "confidence": round(self.confidence, 2),
            "converted": self.converted,
            "unit_found": self.unit_found,
            "qualifier": self.qualifier,
            "hits": self.hits,
        }


# ── 文本准备 ───────────────────────────────────────────────────────────────
_FULLWIDTH = {ord(c): ord(c) - 0xFEE0 for c in "０１２３４５６７８９（）：，．－～"}


def normalize_text(text: str) -> str:
    """全角数字/标点转半角，压缩空白。保留换行，供条件抽取用。"""
    s = text.translate(_FULLWIDTH)
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s


def read_pdf(path: str | Path) -> list[tuple[int, str]]:
    """返回 [(页码, 文本)]。页码从 1 开始。"""
    try:
        import pymupdf  # type: ignore
    except ImportError:  # 兼容旧包名
        import fitz as pymupdf  # type: ignore

    pages: list[tuple[int, str]] = []
    with pymupdf.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            pages.append((i, page.get_text("text") or ""))
    return pages


def read_plain(path: str | Path) -> list[tuple[int, str]]:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return [(1, raw.decode(enc))]
        except UnicodeDecodeError:
            continue
    return [(1, raw.decode("utf-8", errors="replace"))]


def read_document(path: str | Path) -> list[tuple[int, str]]:
    if str(path).lower().endswith(".pdf"):
        return read_pdf(path)
    return read_plain(path)


# ── 别名与单位 ─────────────────────────────────────────────────────────────
def metric_aliases(metric: SpecMetric) -> list[str]:
    names = [metric.name]
    if metric.aliases:
        names += [a.strip() for a in metric.aliases.split(",") if a.strip()]
    # 去掉带括号的修饰，如「压电常数 d33」→ 也接受「d33」
    extra: list[str] = []
    for n in list(names):
        for part in re.findall(r"[A-Za-z][A-Za-z0-9₀-₉/]*|\|?d\d+\|?", n):
            if len(part) >= 2:
                extra.append(part)
    out: list[str] = []
    for n in names + extra:
        n = n.strip().lower()
        if len(n) >= 2 and n not in out:
            out.append(n)
    return out


def _unit_at_start(text: str, unit: str) -> str | None:
    """单位必须紧贴数值出现。返回命中的写法（小写），否则 None。"""
    if not unit:
        return None
    low = text.lower()
    for syn in sorted(UNIT_SYNONYMS.get(unit, ()), key=len, reverse=True):
        if low.startswith(syn):
            return syn
    return None


def _foreign_unit_at_start(text: str, unit: str) -> str | None:
    """紧贴数值的是「别的单位」时返回它（如指标 µL/min、原文 0.7 kPa）。

    存在的意义：_unit_at_start 找不到本指标单位时，旧逻辑会退化成「随便取一个数」，
    于是压力值被当成流量落库。宁可这一处不取，也不能取错。
    """
    low = text.lower()
    own = set(UNIT_SYNONYMS.get(unit, ()))
    for syn in _ALL_UNIT_SYNS:
        # 单字符单位（v / w / g / %）容易撞上单词首字母，只认同长度 ≥ 2 的写法
        if len(syn) < 2 or not low.startswith(syn):
            continue
        nxt = low[len(syn): len(syn) + 1]
        if nxt.isalpha():
            continue  # "watts" 不算 W
        if syn in own:
            return None
        return syn
    return None


def _is_footnote(window: str, m: re.Match) -> bool:
    """(1) 这类脚注标记不是取值。"""
    before = window[m.start() - 1: m.start()] if m.start() > 0 else ""
    after = window[m.end(): m.end() + 1]
    return before == "(" and after == ")"


def _convert(value: float, found_unit: str, target_unit: str) -> tuple[float, bool]:
    fu = found_unit.strip().lower()
    tu = target_unit.strip().lower()
    if not fu or fu == tu:
        return value, False
    factor = UNIT_CONVERT.get((fu, tu))
    if factor is None:
        return value, False
    return value * factor, True


# ── 取值 ───────────────────────────────────────────────────────────────────
def _extract_condition(text: str) -> str | None:
    m = re.search(r"[@＠]\s*[^\s,;，；)]{1,16}", text)
    if m:
        return m.group(0).replace("＠", "@").strip()
    m = re.search(r"\bat\s+[^\s,;)]{1,14}", text, re.IGNORECASE)
    if m:
        return m.group(0).strip()
    return None


def _pack(
    m: re.Match, unit_found: str | None, metric: SpecMetric, window: str
) -> tuple:
    """把一个正则命中整理成 (数值, 文本值, 单位, 是否换算, 比较符, 条件)。"""
    v1 = float(m.group("v1").replace(",", "."))
    v2raw = m.group("v2")
    cmp_sign = (m.group("cmp") or "").strip()
    cond = _extract_condition(window)

    if v2raw:
        v2 = float(v2raw.replace(",", "."))
        if metric.value_type == "number":
            # 数值型指标遇到区间（如 0~70 ℃）：取上限，原文区间写入条件。
            # 上限类指标关心的正是上界；下限类（功耗）取上界也是保守口径。
            upper, _ = _convert(v2, unit_found or metric.unit, metric.unit)
            rng = f"原文 {m.group('v1')}~{v2raw}"
            return (upper, None, unit_found, False, cmp_sign, f"{cmp_sign} {rng}".strip() if cmp_sign else rng)
        return (None, f"{m.group('v1')}~{v2raw}", unit_found, False, cmp_sign, cond)

    value_num, converted = _convert(v1, unit_found or metric.unit, metric.unit)
    if cmp_sign:
        cond = f"{cmp_sign} {cond}".strip() if cond else cmp_sign
    return (value_num, None, unit_found, converted, cmp_sign, cond)


def _find_value(window: str, metric: SpecMetric) -> tuple | None:
    """在标签之后的窗口里找出该指标的取值。

    返回 (value_num, value_text, unit_found, converted, cmp, condition) 或 None。
    取值优先级：紧贴单位的命中 > 第一个可用命中。这样 "(1) ≥0.7 L/min" 会取 0.7 而不是脚注 1。
    """
    if metric.value_type == "text":
        # 文本型：取标签后到行尾/逗号的内容，并剥掉尾随单位（单位单独成列）
        seg = re.split(r"[\n,;，；]", window.lstrip(" :：=\t"), maxsplit=1)[0].strip()
        if metric.unit:
            low = seg.lower()
            for syn in sorted(UNIT_SYNONYMS.get(metric.unit, ()), key=len, reverse=True):
                if low.endswith(syn):
                    seg = seg[: len(seg) - len(syn)].strip()
                    break
        if not seg or len(seg) > 40:
            return None
        return (None, seg, None, False, None, _extract_condition(window))

    fallback: re.Match | None = None
    for m in _RE_VALUE.finditer(window):
        if _is_footnote(window, m):
            continue
        # 数字前若紧跟 ASCII 字母/数字（如 SMD1109、k2），视为型号或标识的一部分，跳过。
        # 注意不能用 isalpha()：中文标签「型」也会返回 True，会把合法取值一起误杀。
        head = window[max(0, m.start() - 1): m.start()]
        if head.isascii() and (head.isalpha() or head.isdigit()):
            continue

        tail = window[m.end(): m.end() + 12]
        gap = len(tail) - len(tail.lstrip(" "))
        # 单位必须紧跟数值（最多隔两个空格），否则会把后面别的数字的单位误挂上来
        adjacent = tail[gap:] if gap <= 2 else ""
        unit_found = _unit_at_start(adjacent, metric.unit) if adjacent else None

        if unit_found is None:
            if adjacent and _foreign_unit_at_start(adjacent, metric.unit):
                # 紧贴的是别的单位（如指标要 µL/min，原文写 kPa）：这不是本指标的取值
                continue
            if fallback is None:
                fallback = m
            continue
        return _pack(m, unit_found, metric, window)

    if fallback is None:
        return None
    return _pack(fallback, None, metric, window)


def _score(
    metric: SpecMetric, unit_found: str | None, cmp_sign: str | None, converted: bool
) -> float:
    if metric.value_type == "text":
        return 0.60
    if unit_found:
        base = 0.90
    elif cmp_sign:
        base = 0.80
    else:
        base = 0.70
    if converted:
        base -= 0.05
    return round(base, 2)


# ── 主流程 ─────────────────────────────────────────────────────────────────
def _alias_boundary_ok(low: str, idx: int, alias: str) -> bool:
    """短 ASCII 别名必须落在词边界上。

    长度 ≤ 4 的英文别名（kp / tc / size / flow）容易命中别的词内部：
    "kp" 会命中 "kPa"、"tc" 会命中 "tcm"。中文别名不受此限制——
    汉字没有词边界概念，卡了反而会漏。
    """
    if len(alias) > 4 or not alias.isascii():
        return True
    before = low[idx - 1] if idx > 0 else ""
    after = low[idx + len(alias): idx + len(alias) + 1]
    for ch in (before, after):
        if ch and (ch.isalnum() or ch == "_"):
            return False
    return True


def extract_candidates(
    pages: list[tuple[int, str]], metrics: list[SpecMetric]
) -> list[Candidate]:
    """从文档中抽取参数候选。metrics 应已按产品大类过滤过。"""
    alias_index: list[tuple[str, SpecMetric]] = []
    for metric in metrics:
        for alias in metric_aliases(metric):
            alias_index.append((alias, metric))
    # 长别名优先，避免「压」抢在「静压」前面
    alias_index.sort(key=lambda pair: len(pair[0]), reverse=True)

    best: dict[str, Candidate] = {}

    for page_no, raw_text in pages:
        text = normalize_text(raw_text)
        low = text.lower()
        # 同一页内已被占用的字符区间。别名之间会重叠（「空气流量」包含「流量」），
        # 不记录区间会把一处出现算成多次命中，也会重复产出候选。
        spans: dict[str, list[tuple[int, int]]] = {}
        for alias, metric in alias_index:
            start = 0
            while True:
                idx = low.find(alias, start)
                if idx < 0:
                    break
                start = idx + len(alias)

                span = (idx, idx + len(alias))
                if not _alias_boundary_ok(low, idx, alias):
                    # 短 ASCII 别名命中单词内部（旧例：kp 命中 kPa），跳过
                    continue
                used = spans.setdefault(metric.key, [])
                if any(s < span[1] and span[0] < e for s, e in used):
                    continue

                following = text[idx + len(alias): idx + len(alias) + 6]
                is_qualifier = any(following.strip().startswith(q) for q in _QUALIFIER_TAIL)

                window = text[idx + len(alias): idx + len(alias) + WINDOW]
                found = _find_value(window, metric)
                if not found:
                    continue

                value_num, value_text, unit_found, converted, cmp_sign, condition = found
                conf = _score(metric, unit_found, cmp_sign, converted)
                if is_qualifier:
                    conf -= 0.15
                if value_text is None and value_num is None:
                    continue

                used.append(span)
                ctx = re.sub(r"\s+", " ", text[max(0, idx - 40): idx + WINDOW]).strip()
                cand = Candidate(
                    metric_key=metric.key,
                    metric_name=metric.name,
                    unit=metric.unit,
                    value_num=round(value_num, 6) if value_num is not None else None,
                    value_text=value_text,
                    matched_label=alias,
                    condition=condition,
                    page=page_no,
                    context=ctx[:160],
                    confidence=round(max(conf, 0.05), 2),
                    converted=converted,
                    unit_found=unit_found,
                    qualifier=is_qualifier,
                )

                key = metric.key
                if key not in best:
                    best[key] = cand
                else:
                    prev = best[key]
                    prev.hits += 1
                    if cand.confidence > prev.confidence:
                        cand.hits = prev.hits
                        best[key] = cand

    return sorted(best.values(), key=lambda c: (-c.confidence, c.metric_key or ""))


def summarize(text_pages: list[tuple[int, str]]) -> dict:
    chars = sum(len(t) for _, t in text_pages)
    return {"pages": len(text_pages), "chars": chars}
