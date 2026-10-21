"""参数对比的纯计算逻辑 —— 不依赖数据库与框架，便于单测。

三条必须守住的规则：
1. 缺失 ≠ 0。未公开的参数显式标记为缺失，参与完整度统计，但不参与「最优值」判定。
2. 最优值只在同类可比时给出。文本型、target 型指标不给最优；只有一个有效值也不给。
3. 差异判定用容差比较，不用字符串。1.50 与 1.5 不应被当成不同。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Product, ProductSpec, SpecMetric
from .timeutil import fmt_local


@dataclass
class Cell:
    product_id: int
    spec_id: int | None
    value_num: float | None
    value_text: str | None
    condition: str | None
    display: str
    is_missing: bool
    verified: bool
    confidence: float
    source_id: int | None
    source_url: str | None = None
    source_title: str | None = None
    captured_at: str | None = None
    is_best: bool = False


@dataclass
class Row:
    metric: SpecMetric
    cells: dict[int, Cell] = field(default_factory=dict)
    best: set[int] = field(default_factory=set)
    differs: bool = False
    has_missing: bool = False


def format_number(value: float, precision: int) -> str:
    if precision <= 0:
        return f"{value:.0f}"
    return f"{value:.{precision}f}"


def compare_key(cell: Cell, metric: SpecMetric) -> str | None:
    """把一格收敛成可比对的键。缺失返回 None。"""
    if cell.is_missing:
        return None
    if metric.value_type == "number" and cell.value_num is not None:
        # 用指标精度做量化，避免 1.5000001 与 1.5 误判为不同
        return format_number(cell.value_num, max(metric.precision, 0))
    return (cell.value_text or "").strip().lower()


def best_product_ids(cells: dict[int, Cell], metric: SpecMetric) -> set[int]:
    """返回该指标下取到最优值的产品 id 集合（并列时返回多个）。"""
    if metric.better not in ("higher", "lower"):
        return set()
    nums = [
        (pid, c.value_num)
        for pid, c in cells.items()
        if not c.is_missing and c.value_num is not None
    ]
    # 少于两个有效值，就谈不上「对比最优」
    if len(nums) < 2:
        return set()

    pick = max if metric.better == "higher" else min
    ref = pick(v for _, v in nums)
    tol = max(abs(ref) * 1e-6, 1e-9)
    return {pid for pid, v in nums if abs(v - ref) <= tol}


def build_matrix(
    products: list[Product],
    metrics: list[SpecMetric],
    specs: list[ProductSpec],
    sources: dict[int, object] | None = None,
) -> list[Row]:
    """组装参数矩阵。行 = 指标，列 = 产品。"""
    sources = sources or {}
    by_key: dict[tuple[int, int], ProductSpec] = {
        (s.product_id, s.metric_id): s for s in specs
    }
    # 未落库的对象没有 id，拿它当 key 会把所有新行并成一格 —— 与其静默错配，
    # 不如直接报错。放在循环外：一次校验就够，不必每个格子都判一遍；
    # 也不用 assert —— python -O 会把 assert 整个剥掉，那时错误变成「KeyError: None」。
    for product in products:
        if product.id is None:
            raise ValueError("build_matrix 收到了未持久化的产品（id 为 None）")

    rows: list[Row] = []

    for metric in metrics:
        row = Row(metric=metric)
        for product in products:
            spec = by_key.get((product.id, metric.id))
            if spec is None:
                cell = Cell(
                    product_id=product.id,
                    spec_id=None,
                    value_num=None,
                    value_text=None,
                    condition=None,
                    display="—",
                    is_missing=True,
                    verified=False,
                    confidence=0.0,
                    source_id=None,
                )
            else:
                if metric.value_type == "number" and spec.value_num is not None:
                    display = format_number(spec.value_num, metric.precision)
                else:
                    display = spec.value_text or "—"
                src = sources.get(spec.source_id) if spec.source_id else None
                cell = Cell(
                    product_id=product.id,
                    spec_id=spec.id,
                    value_num=spec.value_num,
                    value_text=spec.value_text,
                    condition=spec.condition,
                    display=display,
                    is_missing=display == "—",
                    verified=spec.verified,
                    confidence=spec.confidence,
                    source_id=spec.source_id,
                    source_url=getattr(src, "url", None),
                    source_title=getattr(src, "title", None),
                    # 按部署时区显示：库里是 UTC，浮层里直接显示 UTC 会差 8 小时
                    captured_at=fmt_local(spec.captured_at) if spec.captured_at else None,
                )
            row.cells[product.id] = cell

        row.best = best_product_ids(row.cells, metric)
        for pid in row.best:
            row.cells[pid].is_best = True

        row.has_missing = any(c.is_missing for c in row.cells.values())
        keys = {compare_key(c, metric) for c in row.cells.values()}
        keys.discard(None)
        row.differs = len(keys) > 1
        rows.append(row)

    return rows


def completeness(
    rows: list[Row], product_ids: list[int]
) -> dict[int, float]:
    """每个产品的参数完整度（0~1）。这是有情报价值的一列：谁在藏参数。"""
    if not rows:
        return {pid: 0.0 for pid in product_ids}
    out: dict[int, float] = {}
    total = len(rows)
    for pid in product_ids:
        filled = sum(1 for r in rows if not r.cells[pid].is_missing)
        out[pid] = filled / total
    return out


def diff_count(rows: list[Row]) -> int:
    return sum(1 for r in rows if r.differs)


def metric_scope(metric: SpecMetric) -> set[str]:
    return {s.strip() for s in (metric.applies_to or "").split(",") if s.strip()}


# 优劣方向的中文表述。页面、CSV 导出、Excel 导出共用一个口径 ——
# 曾经 CSV 输出 higher/lower、Excel 输出「越大越好」，同一列两种说法。
BETTER_LABEL: dict[str, str] = {
    "higher": "越大越好",
    "lower": "越小越好",
    "target": "取目标值",
    "na": "不比较",
}


def better_label(code: str | None) -> str:
    return BETTER_LABEL.get(code or "", code or "")


def metrics_for_categories(
    metrics: list[SpecMetric], categories: set[str]
) -> list[SpecMetric]:
    """按所选产品的产品大类过滤指标行。

    气泵看流量/静压、风扇看风量/厚度、液泵看 µL 级流量/背压，三套口径不通用。
    若不过滤，跨类对比会退化成一张大面积空白的表，且同类别名会互相串味
    （「流量」既是气泵也是液泵的别名，池子混在一起时一处文本会产出两条互斥候选）。
    规则：applies_to 为空 = 通用；否则与所选类别有交集才保留。
    """
    if not categories:
        return [m for m in metrics if not metric_scope(m)]
    out: list[SpecMetric] = []
    for m in metrics:
        scope = metric_scope(m)
        if not scope or scope & categories:
            out.append(m)
    return out


def metric_applies(metric: SpecMetric, category: str | None) -> bool:
    """这条指标能否写进这个产品大类。

    与 metrics_for_categories 是同一套规则的两个用法：那个决定「页面显示哪些行」，
    这个决定「写入要不要接受」。两者必须同源 —— 早期只有批量接口做了写入校验，
    Excel 导入与规格书提交不做，于是液泵的「液体流量」能写进气泵型号：
    写成功了（报告说 written=1）、但对比页按大类裁剪永远不渲染它，
    变成既看不见又删不掉的幽灵数据。

    category 为空（未分类型号）时不做判断：不能因为分类没填就拒收。
    """
    if not category:
        return True
    scope = metric_scope(metric)
    return not scope or category in scope

