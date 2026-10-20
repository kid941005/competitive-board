"""FastAPI 入口：参数对比页 + JSON API。

页面是服务端渲染（Jinja2 + 原生 JS），P0 阶段刻意不引入前端构建链：
一条 `uvicorn app.main:app` 就能跑起来，便于快速判断对比视图是否真的可用。
P1 若需要更强的表格交互，再把 /api/matrix 换给 Next.js 消费即可，后端契约不变。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlmodel import Session, select

from . import config
from .config import category_label
from .compare import build_matrix, completeness, diff_count, metrics_for_categories
from .datasheet import extract_candidates, read_document, summarize
from .db import get_session, init_db
from .importer import CSV_TEMPLATE, import_csv
from .models import Entity, Product, ProductSpec, SpecMetric, SpecSource

BASE = Path(__file__).resolve().parent
DATASHEET_DIR = config.DATA_DIR / "datasheets"
ALLOWED_DS_EXT = {".pdf", ".txt", ".md", ".csv"}
MAX_DS_BYTES = 25 * 1024 * 1024
DOC_ID_RE = re.compile(r"^[0-9a-f]{12}$")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield

app = FastAPI(title="竞品情报看板 · 参数对比", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# ── 数据装载 ────────────────────────────────────────────────────────────────
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


def _default_selection(products: list[Product], entities: list[Entity]) -> list[int]:
    """默认视图：取型号最多的产品大类，跨友商各取最多 2 个型号。

    刻意不跨大类 —— 压电陶瓷与压电泵的指标不通用，混在一张表里只会看到空白。
    """
    counts: dict[str, int] = {}
    for p in products:
        if p.category:
            counts[p.category] = counts.get(p.category, 0) + 1
    if not counts:
        return [p.id for p in products[:4] if p.id]  # type: ignore[misc]

    dominant = max(counts.items(), key=lambda kv: kv[1])[0]
    pool = [p for p in products if p.category == dominant]

    picked: list[int] = []
    per_entity: dict[int, int] = {}
    for p in pool:
        if len(picked) >= 4:
            break
        if per_entity.get(p.entity_id, 0) >= 2:
            continue
        picked.append(p.id)  # type: ignore[arg-type]
        per_entity[p.entity_id] = per_entity.get(p.entity_id, 0) + 1
    return picked[: config.MAX_COMPARE]


def _parse_ids(raw: str | None, products: list[Product], entities: list[Entity]) -> list[int]:
    valid = {p.id for p in products}
    if not raw:
        return _default_selection(products, entities)
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk.isdigit():
            continue
        pid = int(chunk)
        if pid in valid and pid not in out:
            out.append(pid)
    return out[: config.MAX_COMPARE] or _default_selection(products, entities)


def _matrix_payload(session: Session, ids: list[int]) -> dict:
    entities, products, metrics, sources = _load(session)
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
        "kind_label": {"self": "本方", "competitor": "友商", "customer": "客户"},
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
        "max_compare": config.MAX_COMPARE,
    }


# ── 页面 ───────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/compare")


@app.get("/compare", response_class=HTMLResponse)
def compare_page(
    request: Request,
    p: str | None = Query(default=None),
    only: str = Query(default="all"),
    cat: str = Query(default=""),
    session: Session = Depends(get_session),
):
    entities, products, metrics, _ = _load(session)
    if not products:
        return templates.TemplateResponse(
            request=request, name="empty.html", context={"counts": {"metrics": len(metrics)}}
        )
    ids = _parse_ids(p, products, entities)
    data = _matrix_payload(session, ids)
    data.update({"only": only, "cat": cat})
    return templates.TemplateResponse(request=request, name="compare.html", context=data)


# ── JSON API（供后续前端替换 / MCP 复用）────────────────────────────────────
@app.get("/api/matrix")
def api_matrix(
    p: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    entities, products, _, _ = _load(session)
    ids = _parse_ids(p, products, entities)
    data = _matrix_payload(session, ids)
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


@app.get("/api/metrics")
def api_metrics(session: Session = Depends(get_session)):
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


@app.get("/api/products")
def api_products(session: Session = Depends(get_session)):
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


@app.get("/api/trace/{spec_id}")
def api_trace(spec_id: int, session: Session = Depends(get_session)):
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


# ── 导入 / 导出 ────────────────────────────────────────────────────────────
@app.post("/api/import")
async def api_import(file: UploadFile, session: Session = Depends(get_session)):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    report = import_csv(session, text)
    return report.as_dict()


@app.get("/api/import/template")
def api_import_template():
    return StreamingResponse(
        io.BytesIO(CSV_TEMPLATE.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=spec-import-template.csv"},
    )


@app.get("/export.csv")
def export_csv(
    p: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    entities, products, _, _ = _load(session)
    ids = _parse_ids(p, products, entities)
    data = _matrix_payload(session, ids)

    buf = io.StringIO()
    buf.write("\ufeff")  # BOM，Excel 打开不乱码
    w = csv.writer(buf)
    header = ["指标", "单位", "优劣方向"]
    header += [f"{data['ent_by_id'][pr.entity_id].name} {pr.model}" for pr in data["products"]]
    w.writerow(header)
    for row in data["rows"]:
        line = [row.metric.name, row.metric.unit, row.metric.better]
        for pr in data["products"]:
            cell = row.cells[pr.id]
            line.append("" if cell.is_missing else cell.display)
        w.writerow(line)

    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=spec-compare.csv"},
    )


# ── 规格书导入：自动识别参数 ───────────────────────────────────────────────
class CommitRow(BaseModel):
    metric_key: str
    value_num: float | None = None
    value_text: str | None = None
    condition: str | None = None
    page: int | None = None
    confidence: float = 1.0


class CommitPayload(BaseModel):
    doc_id: str
    product_id: int
    rows: list[CommitRow]


def _ds_meta(doc_id: str) -> dict:
    meta_path = DATASHEET_DIR / f"{doc_id}.json"
    if not meta_path.exists():
        raise HTTPException(404, "文档不存在或已过期，请重新上传")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _ds_file(doc_id: str) -> Path:
    meta = _ds_meta(doc_id)
    path = DATASHEET_DIR / f"{doc_id}{meta.get('ext', '')}"
    if not path.exists():
        raise HTTPException(404, "文档文件已丢失，请重新上传")
    return path


def _check_doc_id(doc_id: str) -> None:
    if not DOC_ID_RE.match(doc_id or ""):
        raise HTTPException(400, "非法的文档标识")


@app.get("/datasheet", response_class=HTMLResponse)
def datasheet_page(
    request: Request,
    product_id: int | None = Query(default=None),
    session: Session = Depends(get_session),
):
    entities, products, metrics, _ = _load(session)
    ent_by_id = {e.id: e for e in entities}
    groups = []
    for code, label in config.CATEGORY_LABEL.items():
        items = [p for p in products if p.category == code]
        if items:
            groups.append({"code": code, "label": label, "items": items})
    return templates.TemplateResponse(
        request=request,
        name="datasheet.html",
        context={
            "groups": groups,
            "ent_by_id": ent_by_id,
            "product_id": product_id,
            "metric_total": len(metrics),
        },
    )


@app.post("/api/datasheet/analyze")
async def api_ds_analyze(
    file: UploadFile,
    product_id: int = Form(...),
    session: Session = Depends(get_session),
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "产品不存在")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    if len(raw) > MAX_DS_BYTES:
        raise HTTPException(413, "文件超过 25 MB")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_DS_EXT:
        raise HTTPException(400, f"暂不支持 {ext or '未知'} 格式，请上传 PDF 或纯文本")

    doc_id = hashlib.sha256(raw).hexdigest()[:12]
    DATASHEET_DIR.mkdir(parents=True, exist_ok=True)
    (DATASHEET_DIR / f"{doc_id}{ext}").write_bytes(raw)
    (DATASHEET_DIR / f"{doc_id}.json").write_text(
        json.dumps(
            {
                "filename": file.filename,
                "ext": ext,
                "size": len(raw),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    pages = read_document(DATASHEET_DIR / f"{doc_id}{ext}")
    _, _, metrics, _ = _load(session)
    applicable = (
        metrics_for_categories(metrics, {product.category})
        if product.category
        else metrics
    )
    candidates = extract_candidates(pages, applicable)
    stats = summarize(pages)

    return {
        "doc_id": doc_id,
        "filename": file.filename,
        "product": {
            "id": product.id,
            "model": product.model,
            "category": product.category,
            "category_label": category_label(product.category),
        },
        "pages": stats["pages"],
        "chars": stats["chars"],
        "metric_total": len(applicable),
        "candidates": [c.to_dict() for c in candidates],
        "metrics": [
            {
                "key": m.key,
                "name": m.name,
                "unit": m.unit,
                "value_type": m.value_type,
            }
            for m in applicable
        ],
    }


@app.post("/api/datasheet/commit")
def api_ds_commit(payload: CommitPayload, session: Session = Depends(get_session)):
    _check_doc_id(payload.doc_id)
    meta = _ds_meta(payload.doc_id)
    product = session.get(Product, payload.product_id)
    if not product:
        raise HTTPException(404, "产品不存在")
    if not payload.rows:
        raise HTTPException(400, "没有要写入的参数")

    source = SpecSource(
        kind="datasheet",
        url=f"/datasheet/{payload.doc_id}/file",
        title=meta.get("filename") or f"{payload.doc_id}",
        captured_at=datetime.now(timezone.utc),
        note=f"规格书自动识别，{len(payload.rows)} 项待复核",
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    metrics = {m.key: m for m in session.exec(select(SpecMetric)).all()}
    written = updated = skipped = 0
    errors: list[str] = []

    for row in payload.rows:
        metric = metrics.get(row.metric_key)
        if metric is None:
            errors.append(f"指标 {row.metric_key} 不在字典中，已跳过")
            skipped += 1
            continue
        is_num = metric.value_type == "number"
        if is_num and row.value_num is None:
            errors.append(f"{metric.name}：缺少数值，已跳过")
            skipped += 1
            continue
        if not is_num and not (row.value_text or "").strip():
            errors.append(f"{metric.name}：缺少文本值，已跳过")
            skipped += 1
            continue

        existing = session.exec(
            select(ProductSpec).where(
                ProductSpec.product_id == product.id,
                ProductSpec.metric_id == metric.id,
            )
        ).first()
        if existing:
            existing.value_num = row.value_num if is_num else None
            existing.value_text = None if is_num else row.value_text
            existing.condition = row.condition or None
            existing.source_id = source.id
            existing.confidence = min(max(row.confidence, 0.0), 1.0)
            existing.verified = False
            existing.updated_at = datetime.now(timezone.utc)
            session.add(existing)
            updated += 1
        else:
            session.add(
                ProductSpec(
                    product_id=product.id,
                    metric_id=metric.id,
                    value_num=row.value_num if is_num else None,
                    value_text=None if is_num else row.value_text,
                    condition=row.condition or None,
                    source_id=source.id,
                    confidence=min(max(row.confidence, 0.0), 1.0),
                    verified=False,
                )
            )
            written += 1

    session.commit()
    return {
        "written": written,
        "updated": updated,
        "skipped": skipped,
        "source_id": source.id,
        "product": product.model,
        "errors": errors,
    }


@app.get("/datasheet/{doc_id}/file")
def ds_file(doc_id: str):
    _check_doc_id(doc_id)
    path = _ds_file(doc_id)
    meta = _ds_meta(doc_id)
    media = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media, filename=meta.get("filename") or path.name)


@app.get("/healthz")
def healthz():
    return {"ok": True}
