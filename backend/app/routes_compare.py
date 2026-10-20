"""对比页、只读 JSON 接口与导出。"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlmodel import Session, select

from . import settings_store
from .auth import P_EXPORT, P_VIEW, AuthSession, User
from .compare import better_label
from .config import category_label
from .db import get_session
from .exporters import matrix_xlsx
from .models import Product, ProductSpec, SpecMetric, SpecSource
from .selection import _load, _matrix_payload, _parse_ids
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


# ── JSON API（只读，需要 data:view）────────────────────────────────────────


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
        "products": [
            {
                "id": pr.id,
                "model": pr.model,
                "name": pr.name,
                "entity": data["ent_by_id"][pr.entity_id].name,
                "entity_kind": data["ent_by_id"][pr.entity_id].kind,
                "completeness": round(data["completeness"].get(pr.id, 0), 4),
            }
            for pr in data["products"]
        ],
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
        "captured_at": spec.captured_at.isoformat() if spec.captured_at else None,
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
    w.writerow(header)
    for row in data["rows"]:
        line = [row.metric.name, row.metric.unit, better_label(row.metric.better)]
        for pr in data["products"]:
            cell = row.cells[pr.id]
            line.append("" if cell.is_missing else cell.display)
        w.writerow(line)

    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=spec-compare.csv"},
    )


# ── 产品管理：管理员直接改数据 ─────────────────────────────────────────────
