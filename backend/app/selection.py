"""选中与矩阵组装 —— 页面、导出、/api/v1 共用同一套计算。

单独成模块的直接原因：`api_v1` 曾经 `from .main import _matrix_payload`，
为绕开循环依赖还在函数体里做局部导入。下沉之后依赖是单向的：
    routes_* / api_v1  →  selection  →  compare / models
而且「人看到的」与「AI 看到的」矩阵从此只有一份实现。
"""

from __future__ import annotations

from sqlmodel import Session, select

from . import config
from .compare import build_matrix, completeness, diff_count, metrics_for_categories
from .config import category_label
from .models import Entity, Product, ProductSpec, SpecMetric, SpecSource


def _load(session: Session):
    entities = session.exec(select(Entity)).all()
    products = session.exec(select(Product)).all()
    metrics = session.exec(
        select(SpecMetric).order_by(SpecMetric.display_order, SpecMetric.id)
    ).all()
    sources = {s.id: s for s in session.exec(select(SpecSource)).all()}

    ent_order = {"self": 0, "competitor": 1, "customer": 2}
    entities.sort(key=lambda e: (ent_order.get(e.kind, 9), e.tier, e.id or 0))
    ent_rank = {e.id: i for i, e in enumerate(entities)}
    products.sort(key=lambda p: (ent_rank.get(p.entity_id, 99), p.model))
    return entities, products, metrics, sources


# 首屏默认只放 4 列：横向可读性优先。想要更多可以自己勾到 max_compare，
# 但打开页面就塞 8 列在窄屏上是没法看的。
DEFAULT_VIEW_MAX = 4


def _default_selection(
    products: list[Product],
    preferred_cat: str | None = None,
    limit: int = DEFAULT_VIEW_MAX,
) -> list[int]:
    """默认视图：优先取管理员配置的大类，同一家公司最多 2 个型号，总数不超过上限。

    「每家 2 个」是为了横向比较时不被某一家刷屏；总数上限取 min(limit, 4) ——
    原实现把 4 写死在循环里，传 limit=8 也只出 4 个，与参数语义不符。
    刻意不跨大类：泵与风扇的指标口径不通用，混在一张表里只会看到空白。
    """
    counts: dict[str, int] = {}
    for p in products:
        if p.category:
            counts[p.category] = counts.get(p.category, 0) + 1
    if not counts:
        cap = max(1, min(limit, DEFAULT_VIEW_MAX))
        return [p.id for p in products[:cap] if p.id]  # type: ignore[misc]

    if preferred_cat and preferred_cat in counts:
        chosen_cat = preferred_cat
    else:
        chosen_cat = max(counts.items(), key=lambda kv: kv[1])[0]
    pool = [p for p in products if p.category == chosen_cat] or products

    cap = max(1, min(limit, DEFAULT_VIEW_MAX))
    picked: list[int] = []
    per_entity: dict[int, int] = {}
    for p in pool:
        if len(picked) >= cap:
            break
        if per_entity.get(p.entity_id, 0) >= 2:
            continue
        picked.append(p.id)  # type: ignore[arg-type]
        per_entity[p.entity_id] = per_entity.get(p.entity_id, 0) + 1
    return picked


def _parse_ids(
    raw: str | None,
    products: list[Product],
    preferred_cat: str | None = None,
    limit: int = DEFAULT_VIEW_MAX,
) -> list[int]:
    """解析 ?p=1,2,3。参数非法或全部无效时回落默认视图 —— 宁可显示默认 4 列，
    也不要给一张空表（前端勾选逻辑假设至少有一列）。"""
    valid = {p.id for p in products}
    if not raw:
        return _default_selection(products, preferred_cat, limit)
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk.isdigit():
            continue
        pid = int(chunk)
        if pid in valid and pid not in out:
            out.append(pid)
    # 回落也要带 limit：否则 CIB_MAX_COMPARE=2 时，一个非法的 ?p= 会给出 4 列
    return out[:limit] or _default_selection(products, preferred_cat, limit)


def _matrix_payload(session: Session, ids: list[int], sset: dict, loaded=None) -> dict:
    # loaded：页面/接口已经 _load 过一次时直接传进来。原来这里无条件再查一遍，
    # /compare 因此产生 13 次 SELECT，其中 4 次是重复的全表读取。
    entities, products, metrics, sources = loaded or _load(session)
    chosen = [p for p in products if p.id in ids]
    order = {pid: i for i, pid in enumerate(ids)}
    chosen.sort(key=lambda p: order.get(p.id or -1, 99))

    # 按所选产品的产品大类裁剪指标行：陶瓷与泵的参数体系不通用
    chosen_cats = {p.category for p in chosen if p.category}
    metrics = metrics_for_categories(metrics, chosen_cats)

    specs = (
        session.exec(
            select(ProductSpec).where(ProductSpec.product_id.in_(ids))  # type: ignore[attr-defined]
        ).all()
        if ids
        else []
    )
    rows = build_matrix(chosen, metrics, specs, sources=sources)
    comp = completeness(rows, ids)

    ent_by_id = {e.id: e for e in entities}
    categories = list(dict.fromkeys(m.category for m in metrics))
    return {
        "entities": entities,
        "ent_by_id": ent_by_id,
        "categories": categories,
        "kind_label": {"self": "自研", "competitor": "友商", "customer": "客户"},
        "cat_label": {code: category_label(code) for code in set(chosen_cats)},
        "cat_all": config.CATEGORY_LABEL,
        "chosen_cats": sorted(chosen_cats),
        "has_self": any(
            ent_by_id[p.entity_id].kind == "self"
            for p in products
            if p.entity_id in ent_by_id
        ),
        "products": chosen,
        "all_products": products,
        "rows": rows,
        "metrics": metrics,
        "completeness": comp,
        "diff_total": diff_count(rows),
        "selected": ids,
        "max_compare": sset["compare.max_products"],
        "self_name": _self_name(session),
    }


# ── 登录 / 登出 ────────────────────────────────────────────────────────────


def _self_name(session: Session) -> str:
    """本方公司名。

    页面与提示语一律用真实公司名，不出现「本方 / 我方」这类代称 ——
    代称在多人协作时没有信息量，而公司名本身就是要看的东西。
    """
    ent = session.exec(select(Entity).where(Entity.kind == "self")).first()
    return ent.name if ent else "左蓝微"
