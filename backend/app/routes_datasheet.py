"""规格书导入：上传 → 识别 → 人工复核 → 落到参数表。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from . import audit, auth, config, settings_store
from .auth import P_VIEW, P_WRITE, AuthSession, User
from .compare import metric_applies, metrics_for_categories
from .config import category_label
from .datasheet import extract_candidates, read_document, summarize
from .db import get_session
from .models import Entity, Product, ProductSpec, SpecMetric, SpecSource
from .numeric import clamp_confidence
from .selection import _load, _self_name
from .web_shared import (
    _ctx,
    _find_company,
    _find_product,
    _perm,
    _perm_user,
    _rid,
    _validate_target,
    templates,
)

DATASHEET_DIR = config.DATASHEET_DIR
ALLOWED_DS_EXT = {".pdf", ".txt", ".md", ".csv"}
DOC_ID_RE = re.compile(r"^[0-9a-f]{12}$")

router = APIRouter()

class CommitRow(BaseModel):
    metric_key: str
    value_num: float | None = None
    value_text: str | None = None
    condition: str | None = None
    page: int | None = None
    confidence: float = 1.0


class CommitTarget(BaseModel):
    """导入目标：公司 + 大类 + 型号。

    导入的用途是**扩充数据库**，所以型号允许不存在 —— 提交时就地建档。
    公司同理：entity_id 为空时按 entity_name 新建（近名会被拦一次，防重复档案）。
    """

    entity_id: int | None = None
    entity_name: str | None = None
    category: str
    model: str
    force_new_entity: bool = False


class CommitPayload(BaseModel):
    doc_id: str
    target: CommitTarget
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


@router.get("/datasheet", response_class=HTMLResponse)
def datasheet_page(
    request: Request,
    entity_id: int | None = Query(default=None),
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    entities, products, metrics, _ = _load(session)

    # 只列本方与友商：客户不会提供竞品规格书，列出来只会选错
    companies = [e for e in entities if e.kind in ("self", "competitor")]
    companies.sort(key=lambda e: (0 if e.kind == "self" else 1, e.name))

    return templates.TemplateResponse(
        request=request,
        name="datasheet.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="导入规格书",
            page_sub="上传 PDF / 文本 → 自动识别参数 → 人工复核 → 入库（标记未复核）",
            active="datasheet",
            companies=companies,
            products=products,
            product_index=json.dumps(
                [
                    {"entity_id": p.entity_id, "model": p.model, "category": p.category or ""}
                    for p in products
                ],
                ensure_ascii=False,
            ).replace("</", "<\\/"),
            categories=list(config.CATEGORY_LABEL.items()),
            entity_id=entity_id,
            metric_total=len(metrics),
            product_total=len(products),
            min_confidence=sset["datasheet.min_confidence"],
            max_upload_mb=sset["datasheet.max_upload_mb"],
        ),
    )


@router.post("/api/datasheet/analyze")
async def api_ds_analyze(
    file: UploadFile,
    entity_id: int | None = Form(default=None),
    entity_name: str | None = Form(default=None),
    category: str = Form(...),
    model: str = Form(...),
    _csrf: None = Depends(auth.verify_csrf),
    _user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    model = _validate_target(category, model)

    entity: Entity | None = None
    if entity_id is not None:
        entity = session.get(Entity, entity_id)
        if not entity:
            raise HTTPException(404, "公司不存在，请刷新页面重选")
        if entity.kind == "customer":
            raise HTTPException(400, f"客户不是竞品来源，请选择友商或{_self_name(session)}")
    else:
        name = (entity_name or "").strip()
        if not name:
            raise HTTPException(400, "请选择公司，或填写新公司名称")
        # 同名直接复用；近名在提交时才拦，此处只做归属判断，不落库
        entity, _ = _find_company(session, name)

    product = _find_product(session, entity.id, model) if entity and entity.id else None

    sset = settings_store.all_values(session)
    max_bytes = int(sset["datasheet.max_upload_mb"]) * 1024 * 1024

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    if len(raw) > max_bytes:
        raise HTTPException(413, f"文件超过 {sset['datasheet.max_upload_mb']} MB")

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
    _, _, all_metrics, _ = _load(session)
    applicable = metrics_for_categories(all_metrics, {category})
    candidates = extract_candidates(pages, applicable)
    stats = summarize(pages)

    warning = None
    if product and product.category and product.category != category:
        warning = (
            f"型号 {product.model} 已存在且属于「{category_label(product.category)}」，"
            "与所选大类不一致，提交会被拒绝。请改选大类或换一个型号名。"
        )

    return {
        "doc_id": doc_id,
        "filename": file.filename,
        "target": {
            "entity_id": entity.id if entity else None,
            "entity_name": entity.name if entity else (entity_name or "").strip(),
            "entity_is_new": entity is None,
            "category": category,
            "category_label": category_label(category),
            "model": model,
            "product_id": product.id if product else None,
            "product_is_new": product is None,
        },
        "warning": warning,
        "pages": stats["pages"],
        "chars": stats["chars"],
        "metric_total": len(applicable),
        "candidates": [c.to_dict() for c in candidates],
        "metrics": [
            {"key": m.key, "name": m.name, "unit": m.unit, "value_type": m.value_type}
            for m in applicable
        ],
    }


@router.post("/api/datasheet/commit")
def api_ds_commit(
    payload: CommitPayload,
    _csrf: None = Depends(auth.verify_csrf),
    user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    _check_doc_id(payload.doc_id)
    meta = _ds_meta(payload.doc_id)
    target = payload.target
    model = _validate_target(target.category, target.model)
    if not payload.rows:
        raise HTTPException(400, "没有要写入的参数")

    # ── 1) 定位公司，必要时就地建档 ──────────────────────────────────
    entity: Entity | None = None
    entity_created = False
    if target.entity_id is not None:
        entity = session.get(Entity, target.entity_id)
        if not entity:
            raise HTTPException(404, "公司不存在，请刷新页面重选")
        if entity.kind == "customer":
            raise HTTPException(400, f"客户不是竞品来源，请选择友商或{_self_name(session)}")
    else:
        name = (target.entity_name or "").strip()
        if not name:
            raise HTTPException(400, "请选择公司，或填写新公司名称")
        exact, similar = _find_company(session, name)
        if exact:
            entity = exact
        elif similar and not target.force_new_entity:
            names = "、".join(f"「{e.name}」" for e in similar[:3])
            raise HTTPException(
                409,
                f"已存在相似公司{names}。同一家因写法不同建出两份档案后，"
                "参数会分散在两处、对比时也聚合不到一起。"
                "请返回选择已有公司；若确认是另一家，勾选「确认新建独立档案」后重试。",
            )
        else:
            entity = Entity(kind="competitor", name=name, tier="C")
            session.add(entity)
            session.flush()
            entity_created = True

    # ── 2) 定位型号，必要时就地建档 ──────────────────────────────────
    product = _find_product(session, entity.id, model)
    product_created = False
    if product:
        if product.category and product.category != target.category:
            raise HTTPException(
                409,
                f"型号 {product.model} 已存在且属于「{category_label(product.category)}」，"
                f"与所选「{category_label(target.category)}」不一致。"
                "同一型号不能跨大类，请改选大类或换一个型号名。",
            )
        if not product.category:
            product.category = target.category
            session.add(product)
    else:
        product = Product(
            entity_id=entity.id,  # type: ignore[arg-type]
            model=model,
            name=model,
            category=target.category,
            status="active",
        )
        session.add(product)
        session.flush()
        product_created = True

    # ── 3) 出处 ──────────────────────────────────────────────────────
    source = SpecSource(
        kind="datasheet",
        url=f"/datasheet/{payload.doc_id}/file",
        title=meta.get("filename") or f"{payload.doc_id}",
        captured_at=datetime.now(timezone.utc),
        note=f"规格书自动识别，{len(payload.rows)} 项待复核；录入人 {user.username}",
    )
    session.add(source)
    session.flush()

    metrics = {m.key: m for m in session.exec(select(SpecMetric)).all()}
    metric_by_id = {m.id: m for m in metrics.values()}
    before_specs = audit.spec_snapshot(session, product.id, metric_by_id)
    written = updated = skipped = 0
    errors: list[str] = []
    # 复核表允许把两行的指标改成同一个，或识别出重复项。数据库有
    # (型号, 指标) 唯一索引，所以这里要维护「本批已写入的格子」，
    # 第二次命中就走更新，而不是再插一条。
    present = {
        s.metric_id: s
        for s in session.exec(
            select(ProductSpec).where(ProductSpec.product_id == product.id)
        ).all()
    }

    for row in payload.rows:
        metric = metrics.get(row.metric_key)
        if metric is None:
            errors.append(f"指标 {row.metric_key} 不在字典中，已跳过")
            skipped += 1
            continue
        # 与批量接口、表格导入同一条规则：指标必须适用于该型号的大类，
        # 否则会写进一条「永远不显示」的幽灵参数（对比页按大类裁剪指标行）。
        if not metric_applies(metric, product.category):
            errors.append(
                f"{metric.name} 不适用于「{category_label(product.category)}」，已跳过"
            )
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

        existing = present.get(metric.id)  # type: ignore[arg-type]
        if existing:
            existing.value_num = row.value_num if is_num else None
            existing.value_text = None if is_num else row.value_text
            existing.condition = row.condition or None
            existing.source_id = source.id
            existing.confidence = clamp_confidence(row.confidence)
            existing.verified = False
            existing.updated_at = datetime.now(timezone.utc)
            session.add(existing)
            updated += 1
        else:
            created = ProductSpec(
                product_id=product.id,
                metric_id=metric.id,
                value_num=row.value_num if is_num else None,
                value_text=None if is_num else row.value_text,
                condition=row.condition or None,
                source_id=source.id,
                confidence=clamp_confidence(row.confidence),
                verified=False,
            )
            session.add(created)
            present[metric.id] = created  # type: ignore[index]
            written += 1

    rid = _rid()
    if entity_created:
        audit.record(
            session, actor=user.username, action="create", object_type="entity",
            object_id=entity.id, label=entity.name, origin="datasheet",
            changes=[{"field": "公司", "old": None, "new": entity.name}],
            note="由规格书导入自动建档", request_id=rid,
        )
    if product_created:
        audit.record(
            session, actor=user.username, action="create", object_type="product",
            object_id=product.id, label=product.model, origin="datasheet",
            changes=audit.diff({}, audit.product_snapshot(product), audit.PRODUCT_LABELS),
            note="由规格书导入自动建档", request_id=rid,
        )
    audit.record(
        session, actor=user.username, action="update", object_type="spec",
        object_id=product.id, label=product.model, origin="datasheet",
        changes=audit.diff(before_specs, audit.spec_snapshot(session, product.id, metric_by_id)),
        note=f"规格书 {meta.get('filename') or payload.doc_id}：写入 {written}、更新 {updated}、跳过 {skipped}",
        request_id=rid,
    )
    session.commit()
    return {
        "written": written,
        "updated": updated,
        "skipped": skipped,
        "source_id": source.id,
        "entity": entity.name,
        "entity_created": entity_created,
        "product": product.model,
        "product_created": product_created,
        "category_label": category_label(product.category),
        "errors": errors,
    }


@router.get("/datasheet/{doc_id}/file")
def ds_file(doc_id: str, _pair=Depends(_perm(P_VIEW))):
    _check_doc_id(doc_id)
    path = _ds_file(doc_id)
    meta = _ds_meta(doc_id)
    media = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media, filename=meta.get("filename") or path.name)


# ── 导入 / 导出 ────────────────────────────────────────────────────────────
SHEET_EXT = {".xlsx", ".xlsm", ".csv", ".txt"}
