"""对比页、只读 JSON 接口与导出。"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlmodel import Session, select

from . import backup, config, settings_store
from .auth import P_EXPORT, P_VIEW, AuthSession, User
from .compare import better_label
from .config import category_label
from .db import get_session
from .export_safety import csv_text
from .exporters import matrix_xlsx
from .models import Product, ProductSpec, SpecMetric, SpecSource
from .selection import _load, _matrix_payload, _parse_ids
from .timeutil import iso_utc
from .web_shared import _ctx, _perm, templates

router = APIRouter()


@router.get("/compare", response_class=HTMLResponse)
def compare_page(
    request: Request,
    p: str | None = Query(default=None),
    only: str = Query(default="all"),
    cat: str = Query(default=""),
    pair: tuple[User, AuthSession] = Depends(_perm(P_VIEW)),
    session: Session = Depends(get_session),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    loaded = _load(session)
    _, products, metrics, _ = loaded
    if not products:
        return templates.TemplateResponse(
            request=request,
            name="empty.html",
            context=_ctx(
                user,
                auth_session,
                sset,
                page_title="竞品参数对比",
                page_sub="暂无产品数据",
                active="compare",
                counts={"metrics": len(metrics)},
            ),
        )

    ids = _parse_ids(
        p,
        products,
        preferred_cat=sset["ui.default_category"],
        limit=sset["compare.max_products"],
    )
    data = _matrix_payload(session, ids, sset, loaded=loaded)
    data["picker_groups"] = _picker_groups(products)
    # ?cat= 指向一个当前视图里不存在的指标组时回落「全部」，否则页面上没有任何一个
    # tab 是选中的，看起来像样式坏了
    if cat and cat not in data["categories"]:
        cat = ""
    data.update(
        _ctx(
            user,
            auth_session,
            sset,
            page_title="竞品参数对比",
            active="compare",
            only=only,
            cat=cat,
        )
    )
    cats_txt = " / ".join(category_label(c) for c in data["chosen_cats"]) or "全部大类"
    data["page_sub"] = (
        f"{cats_txt} · {len(data['products'])} 个型号 · {len(data['rows'])} 项指标"
    )
    return templates.TemplateResponse(request=request, name="compare.html", context=data)


def _picker_groups(products: list[Product]) -> list[dict]:
    """型号选择器的分组：按压电气泵 / 压电风扇 / 压电液泵分段，未分类单列一段。

    在 Python 里分好组，模板只写一遍 chip 结构 —— 之前模板里把同一段 markup 复制了
    两份（有分类 / 未分类），改一处漏一处是迟早的事。
    """
    groups: list[dict] = []
    for code, label in config.CATEGORY_LABEL.items():
        items = [p for p in products if p.category == code]
        if items:
            # hint 一律给键（哪怕是空串）：模板里 `{% if g.hint %}` 在
            # StrictUndefined 下对缺键会直接抛错 —— 生产环境宽松所以看不出来，
            # 测试一开严格模式就是 500。
            groups.append({"code": code, "label": label, "products": items, "hint": ""})
    nocat = [p for p in products if not p.category]
    if nocat:
        groups.append({
            "code": "", "label": "未分类", "products": nocat,
            "hint": '去<a href="/products">产品管理</a>补大类',
        })
    return groups


# ── JSON API（只读，需要 data:view）────────────────────────────────────────


def _matrix_product_row(product: Product, data: dict) -> dict:
    """矩阵响应里的一个型号。公司被并发删除时给 None，而不是 KeyError。"""
    ent = data["ent_by_id"].get(product.entity_id)
    return {
        "id": product.id,
        "model": product.model,
        "name": product.name,
        "entity": ent.name if ent else None,
        "entity_kind": ent.kind if ent else None,
        "completeness": round(data["completeness"].get(product.id, 0), 4),
    }


@router.get("/api/matrix")
def api_matrix(
    p: str | None = Query(default=None),
    _pair=Depends(_perm(P_VIEW)),
    session: Session = Depends(get_session),
):
    sset = settings_store.all_values(session)
    loaded = _load(session)
    _, products, _, _ = loaded
    ids = _parse_ids(
        p,
        products,
        preferred_cat=sset["ui.default_category"],
        limit=sset["compare.max_products"],
    )
    data = _matrix_payload(session, ids, sset, loaded=loaded)
    payload = {
        "selected": ids,
        "products": [_matrix_product_row(pr, data) for pr in data["products"]],
        "rows": [
            {
                "metric": r.metric.name,
                "key": r.metric.key,
                "category": r.metric.category,
                "unit": r.metric.unit,
                "better": r.metric.better,
                "differs": r.differs,
                "has_missing": r.has_missing,
                "values": {
                    str(pid): {
                        "display": c.display,
                        "num": c.value_num,
                        "text": c.value_text,
                        "best": c.is_best,
                        "missing": c.is_missing,
                        "source_url": c.source_url,
                        "captured_at": c.captured_at,
                    }
                    for pid, c in r.cells.items()
                },
            }
            for r in data["rows"]
        ],
    }
    return JSONResponse(payload)


@router.get("/api/metrics")
def api_metrics(_pair=Depends(_perm(P_VIEW)), session: Session = Depends(get_session)):
    metrics = session.exec(
        select(SpecMetric).order_by(SpecMetric.display_order, SpecMetric.id)
    ).all()
    return [
        {
            "key": m.key,
            "category": m.category,
            "name": m.name,
            "unit": m.unit,
            "value_type": m.value_type,
            "better": m.better,
            "precision": m.precision,
            "is_key_metric": m.is_key_metric,
            "applies_to": m.applies_to,
        }
        for m in metrics
    ]


@router.get("/api/products")
def api_products(_pair=Depends(_perm(P_VIEW)), session: Session = Depends(get_session)):
    entities, products, _, _ = _load(session)
    by_id = {e.id: e for e in entities}
    return [
        {
            "id": p.id,
            "model": p.model,
            "name": p.name,
            "entity_id": p.entity_id,
            "entity": by_id[p.entity_id].name if p.entity_id in by_id else None,
            "entity_kind": by_id[p.entity_id].kind if p.entity_id in by_id else None,
            "category": p.category,
            "category_label": category_label(p.category),
        }
        for p in products
    ]


@router.get("/api/trace/{spec_id}")
def api_trace(
    spec_id: int, _pair=Depends(_perm(P_VIEW)), session: Session = Depends(get_session)
):
    spec = session.get(ProductSpec, spec_id)
    if not spec:
        return JSONResponse({"error": "not found"}, status_code=404)
    src = session.get(SpecSource, spec.source_id) if spec.source_id else None
    product = session.get(Product, spec.product_id)
    metric = session.get(SpecMetric, spec.metric_id)
    return {
        "spec_id": spec.id,
        "product": product.model if product else None,
        "metric": metric.name if metric else None,
        "unit": metric.unit if metric else "",
        "value_num": spec.value_num,
        "value_text": spec.value_text,
        "condition": spec.condition,
        "confidence": spec.confidence,
        "verified": spec.verified,
        "captured_at": iso_utc(spec.captured_at),
        "source": {
            "kind": src.kind if src else None,
            "url": src.url if src else None,
            "title": src.title if src else None,
        },
    }


# ── 规格书导入（写操作，需要 data:write）──────────────────────────────────


def _export_dataset(p: str | None, session: Session) -> dict:
    sset = settings_store.all_values(session)
    loaded = _load(session)
    _, products, _, _ = loaded
    ids = _parse_ids(
        p,
        products,
        preferred_cat=sset["ui.default_category"],
        limit=sset["compare.max_products"],
    )
    return _matrix_payload(session, ids, sset, loaded=loaded)


@router.get("/export.xlsx")
def export_xlsx(
    p: str | None = Query(default=None),
    _pair=Depends(_perm(P_EXPORT)),
    session: Session = Depends(get_session),
):
    data = _export_dataset(p, session)
    stamp = datetime.now().strftime("%Y%m%d")
    return StreamingResponse(
        io.BytesIO(matrix_xlsx(data)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=spec-compare-{stamp}.xlsx"},
    )


@router.get("/export-all.xlsx")
def export_all(
    _pair=Depends(_perm(P_EXPORT)),
    session: Session = Depends(get_session),
):
    """导出全部数据（备份用）：参数宽表（可再导入）+ 参数明细 + 客户采用。

    与 `/export.xlsx` 的区别是「范围」而不是「格式」：那边只导当前选中的几个型号，
    是拿来对比的报表；这里不管选择状态，把库里所有型号所有参数都倒出来。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        io.BytesIO(backup.export_all_xlsx(session)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition":
                f"attachment; filename=competitive-board-backup-{stamp}.xlsx"
        },
    )


@router.get("/export.csv")
def export_csv(
    p: str | None = Query(default=None),
    _pair=Depends(_perm(P_EXPORT)),
    session: Session = Depends(get_session),
):
    data = _export_dataset(p, session)

    buf = io.StringIO()
    buf.write("\ufeff")  # BOM，Excel 打开不乱码
    w = csv.writer(buf)
    header = ["指标", "单位", "优劣方向"]
    header += [f"{data['ent_by_id'][pr.entity_id].name} {pr.model}" for pr in data["products"]]
    w.writerow([csv_text(value) for value in header])
    for row in data["rows"]:
        line = [row.metric.name, row.metric.unit, better_label(row.metric.better)]
        for pr in data["products"]:
            cell = row.cells[pr.id]
            line.append("" if cell.is_missing else cell.display)
        w.writerow([csv_text(value) for value in line])

    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=spec-compare.csv"},
    )


# ── 产品管理：管理员直接改数据 ─────────────────────────────────────────────
