"""FastAPI 入口：认证、参数对比、规格书导入、账号与系统参数管理。

页面是服务端渲染（Jinja2 + 原生 JS），刻意不引入前端构建链：
一条 `uvicorn app.main:app` 就能跑起来。P1 若需要更强的表格交互，
把 /api/matrix 换给前端框架消费即可，后端契约不变。

权限边界见 auth.py 的 ROLE_PERMISSIONS —— 本文件的路由只声明「需要哪个权限位」，
不出现任何 role 字符串比较。接口层缺权限一律 403、未登录一律 401（HTML 则跳登录页）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

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

from . import auth, config, settings_store
from .auth import (
    COOKIE_NAME,
    P_EXPORT,
    P_SETTINGS,
    P_USERS,
    P_VIEW,
    P_WRITE,
    AuthSession,
    User,
)
from .config import category_label
from .compare import build_matrix, completeness, diff_count, metrics_for_categories
from .datasheet import extract_candidates, read_document, summarize
from .db import engine, get_session, init_db
from .importer import CSV_TEMPLATE, import_csv
from .models import Entity, Product, ProductSpec, SpecMetric, SpecSource

BASE = Path(__file__).resolve().parent
DATASHEET_DIR = config.DATA_DIR / "datasheets"
ALLOWED_DS_EXT = {".pdf", ".txt", ".md", ".csv"}
DOC_ID_RE = re.compile(r"^[0-9a-f]{12}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

# 用户不存在时也走一次哈希校验，避免通过响应时间枚举账号
_DUMMY_HASH = auth.hash_password("__nonexistent__")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    admin_name = (os.getenv("CIB_ADMIN_USER") or "admin").strip() or "admin"
    with Session(engine) as session:
        auth.purge_expired(session)
        generated = auth.ensure_bootstrap_admin(session)
    if generated:
        # 只在此打印一次。请立即登录后修改密码。
        print("=" * 62, flush=True)
        print(f"  已创建初始管理员：{admin_name}", flush=True)
        print(f"  初始密码：{generated}", flush=True)
        print("  登录后请立刻在「账号管理」里修改密码。", flush=True)
        print("=" * 62, flush=True)
    yield


app = FastAPI(title="竞品情报看板", version="0.3.1", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))
# 静态资源版本号：前端改动后改 app.version 即可让浏览器重新拉取，
# 避免「代码已修复但用户还跑着缓存里的旧脚本」这种假象。
templates.env.globals["asset_v"] = app.version


# ── 认证 / 授权异常：页面层与接口层分别处置 ────────────────────────────────
def _wants_html(request: Request) -> bool:
    """页面返回 HTML、接口返回 JSON —— 按路径判定，不依赖 Accept 头。

    只看 Accept 会造成割裂：浏览器跳登录页，而 curl 拿 401，
    排查问题时很难判断到底是被拦了还是路由不存在。
    """
    path = request.url.path
    if path.startswith("/api/") or path == "/export.csv" or path.endswith("/file"):
        return False
    if path.startswith("/admin/") or path in ("/", "/compare", "/datasheet"):
        return True
    return "text/html" in request.headers.get("accept", "")


@app.exception_handler(auth.NotAuthenticated)
async def _on_unauthenticated(request: Request, _exc: auth.NotAuthenticated):
    if _wants_html(request):
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    return JSONResponse({"detail": "未登录", "code": "unauthenticated"}, status_code=401)


@app.exception_handler(auth.Forbidden)
async def _on_forbidden(request: Request, exc: auth.Forbidden):
    if _wants_html(request):
        return templates.TemplateResponse(
            request=request,
            name="forbidden.html",
            context={
                "required": exc.perm,
                "role": exc.role,
                "role_label": auth.ROLE_LABEL.get(exc.role, exc.role),
            },
            status_code=403,
        )
    return JSONResponse(
        {
            "detail": "权限不足",
            "code": "forbidden",
            "required": exc.perm,
            "role": exc.role,
        },
        status_code=403,
    )


@app.exception_handler(auth.CsrfInvalid)
async def _on_csrf(request: Request, _exc: auth.CsrfInvalid):
    if _wants_html(request):
        return templates.TemplateResponse(
            request=request,
            name="forbidden.html",
            context={
                "required": "csrf",
                "role": "-",
                "role_label": "会话校验失败",
                "hint": "页面可能长时间未刷新，请返回上一页后重试。",
            },
            status_code=403,
        )
    return JSONResponse({"detail": "CSRF 校验失败", "code": "csrf"}, status_code=403)


def _perm(perm: str):
    """要求某权限位的依赖，同时把 (user, auth_session) 交给路由用。"""

    def dependency(
        pair: tuple[User, AuthSession] = Depends(auth.get_current),
    ) -> tuple[User, AuthSession]:
        user, auth_session = pair
        if not auth.has_perm(user, perm):
            raise auth.Forbidden(perm, user.role)
        return pair

    return dependency


def _perm_user(perm: str):
    """只取 User 的版本。写操作用的都是它；页面需要 csrf 时用 _perm 拿元组。"""

    def dependency(pair: tuple[User, AuthSession] = Depends(_perm(perm))) -> User:
        return pair[0]

    return dependency


def _ctx(user: User, auth_session: AuthSession, sset: dict, **extra) -> dict:
    ctx = auth.view_ctx(user, auth_session)
    ctx["sset"] = sset
    ctx.update(extra)
    return ctx


def _safe_next(target: str | None) -> str:
    """只允许站内相对路径，防开放重定向。"""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/compare"
    return target


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


def _default_selection(
    products: list[Product],
    entities: list[Entity],
    preferred_cat: str | None = None,
    limit: int = 8,
) -> list[int]:
    """默认视图：优先取管理员配置的大类，跨友商各取最多 2 个型号。

    刻意不跨大类 —— 压电陶瓷与压电泵的指标不通用，混在一张表里只会看到空白。
    """
    counts: dict[str, int] = {}
    for p in products:
        if p.category:
            counts[p.category] = counts.get(p.category, 0) + 1
    if not counts:
        return [p.id for p in products[:4] if p.id]  # type: ignore[misc]

    if preferred_cat and preferred_cat in counts:
        chosen_cat = preferred_cat
    else:
        chosen_cat = max(counts.items(), key=lambda kv: kv[1])[0]
    pool = [p for p in products if p.category == chosen_cat] or products

    picked: list[int] = []
    per_entity: dict[int, int] = {}
    for p in pool:
        if len(picked) >= 4:
            break
        if per_entity.get(p.entity_id, 0) >= 2:
            continue
        picked.append(p.id)  # type: ignore[arg-type]
        per_entity[p.entity_id] = per_entity.get(p.entity_id, 0) + 1
    return picked[:limit]


def _parse_ids(
    raw: str | None,
    products: list[Product],
    entities: list[Entity],
    preferred_cat: str | None = None,
    limit: int = 8,
) -> list[int]:
    valid = {p.id for p in products}
    if not raw:
        return _default_selection(products, entities, preferred_cat, limit)
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk.isdigit():
            continue
        pid = int(chunk)
        if pid in valid and pid not in out:
            out.append(pid)
    return out[:limit] or _default_selection(products, entities, preferred_cat, limit)


def _matrix_payload(session: Session, ids: list[int], sset: dict) -> dict:
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
        "max_compare": sset["compare.max_products"],
    }


# ── 登录 / 登出 ────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = Query(default="/compare")):
    return templates.TemplateResponse(
        request=request, name="login.html", context={"next": _safe_next(next), "error": None}
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/compare"),
    session: Session = Depends(get_session),
):
    name = username.strip()
    user = session.exec(select(User).where(User.username == name)).first()

    # 无论账号是否存在都做一次校验，抹平时间差
    ok = auth.verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not ok or not user.is_active:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"next": _safe_next(next), "error": "账号或密码错误，或账号已停用"},
            status_code=401,
        )

    hours = int(settings_store.get_value(session, "security.session_hours"))
    token, _csrf = auth.create_session(session, user, hours, request.headers.get("user-agent"))

    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=hours * 3600,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
        path="/",
    )
    return response


@app.post("/logout")
def logout(
    request: Request,
    _csrf: None = Depends(auth.verify_csrf),
    session: Session = Depends(get_session),
):
    auth.revoke_session(session, request.cookies.get(COOKIE_NAME, ""))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


# ── 页面 ───────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def index(request: Request, pair=Depends(_perm(P_VIEW))) -> RedirectResponse:
    return RedirectResponse(url="/compare")


@app.get("/compare", response_class=HTMLResponse)
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
    entities, products, metrics, _ = _load(session)
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
        entities,
        preferred_cat=sset["ui.default_category"],
        limit=sset["compare.max_products"],
    )
    data = _matrix_payload(session, ids, sset)
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
@app.get("/api/matrix")
def api_matrix(
    p: str | None = Query(default=None),
    _pair=Depends(_perm(P_VIEW)),
    session: Session = Depends(get_session),
):
    sset = settings_store.all_values(session)
    entities, products, _, _ = _load(session)
    ids = _parse_ids(
        p,
        products,
        entities,
        preferred_cat=sset["ui.default_category"],
        limit=sset["compare.max_products"],
    )
    data = _matrix_payload(session, ids, sset)
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


@app.get("/api/products")
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


@app.get("/api/trace/{spec_id}")
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
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    entities, products, _, _ = _load(session)
    ent_by_id = {e.id: e for e in entities}
    groups = []
    for code, label in config.CATEGORY_LABEL.items():
        items = [p for p in products if p.category == code]
        if items:
            groups.append({"code": code, "label": label, "items": items})
    return templates.TemplateResponse(
        request=request,
        name="datasheet.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="导入规格书",
            page_sub="上传 PDF / 文本，自动识别参数 → 人工复核 → 入库（标记未复核）",
            active="datasheet",
            groups=groups,
            ent_by_id=ent_by_id,
            product_id=product_id,
            metric_total=len(products),
            product_total=len(products),
            min_confidence=sset["datasheet.min_confidence"],
            max_upload_mb=sset["datasheet.max_upload_mb"],
        ),
    )


@app.post("/api/datasheet/analyze")
async def api_ds_analyze(
    file: UploadFile,
    product_id: int = Form(...),
    _csrf: None = Depends(auth.verify_csrf),
    _user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "产品不存在")

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
    _, _, metrics, _ = _load(session)
    applicable = (
        metrics_for_categories(metrics, {product.category}) if product.category else metrics
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
            {"key": m.key, "name": m.name, "unit": m.unit, "value_type": m.value_type}
            for m in applicable
        ],
    }


@app.post("/api/datasheet/commit")
def api_ds_commit(
    payload: CommitPayload,
    _csrf: None = Depends(auth.verify_csrf),
    user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
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
        note=f"规格书自动识别，{len(payload.rows)} 项待复核；录入人 {user.username}",
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
def ds_file(doc_id: str, _pair=Depends(_perm(P_VIEW))):
    _check_doc_id(doc_id)
    path = _ds_file(doc_id)
    meta = _ds_meta(doc_id)
    media = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media, filename=meta.get("filename") or path.name)


# ── 导入 / 导出 ────────────────────────────────────────────────────────────
@app.post("/api/import")
async def api_import(
    file: UploadFile,
    _csrf: None = Depends(auth.verify_csrf),
    _user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    report = import_csv(session, text)
    return report.as_dict()


@app.get("/api/import/template")
def api_import_template(_pair=Depends(_perm(P_WRITE))):
    return StreamingResponse(
        io.BytesIO(CSV_TEMPLATE.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=spec-import-template.csv"},
    )


@app.get("/export.csv")
def export_csv(
    p: str | None = Query(default=None),
    _pair=Depends(_perm(P_EXPORT)),
    session: Session = Depends(get_session),
):
    sset = settings_store.all_values(session)
    entities, products, _, _ = _load(session)
    ids = _parse_ids(
        p,
        products,
        entities,
        preferred_cat=sset["ui.default_category"],
        limit=sset["compare.max_products"],
    )
    data = _matrix_payload(session, ids, sset)

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


# ── 管理员：账号管理 ───────────────────────────────────────────────────────
def _active_admin_count(session: Session) -> int:
    rows = session.exec(select(User).where(User.role == "admin", User.is_active == True)).all()  # noqa: E712
    return len(rows)


def _redirect(url: str, msg: str | None = None, kind: str = "ok") -> RedirectResponse:
    if msg:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{kind}={quote(msg)}"
    return RedirectResponse(url, status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_USERS)),
    session: Session = Depends(get_session),
    ok: str | None = Query(default=None),
    err: str | None = Query(default=None),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    users = session.exec(select(User).order_by(User.role, User.username)).all()  # type: ignore[attr-defined]
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="账号管理",
            page_sub="新建账号、调整角色、重置密码、停用账号",
            active="users",
            users=users,
            role_options=(
                ("admin", auth.ROLE_LABEL["admin"]),
                ("viewer", auth.ROLE_LABEL["viewer"]),
            ),
            role_permissions={
                role: sorted(perms) for role, perms in auth.ROLE_PERMISSIONS.items()
            },
            active_admin_count=_active_admin_count(session),
            ok=ok,
            err=err,
        ),
    )


@app.post("/admin/users/create")
def admin_create_user(
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(default=""),
    role: str = Form(default="viewer"),
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_USERS)),
    session: Session = Depends(get_session),
):
    name = username.strip()
    if not USERNAME_RE.match(name):
        return _redirect("/admin/users", "用户名需为 3–32 位字母、数字、下划线、点或短横线", "err")
    if role not in auth.ROLE_PERMISSIONS:
        return _redirect("/admin/users", "角色不合法", "err")
    problem = auth.check_password_policy(password)
    if problem:
        return _redirect("/admin/users", problem, "err")
    if session.exec(select(User).where(User.username == name)).first() is not None:
        return _redirect("/admin/users", f"用户名 {name} 已存在", "err")

    session.add(
        User(
            username=name,
            display_name=display_name.strip() or None,
            password_hash=auth.hash_password(password),
            role=role,
            created_by=actor.username,
        )
    )
    session.commit()
    return _redirect("/admin/users", f"已创建 {name}（{auth.ROLE_LABEL[role]}）")


@app.post("/admin/users/{uid}/role")
def admin_set_role(
    uid: int,
    role: str = Form(...),
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_USERS)),
    session: Session = Depends(get_session),
):
    target = session.get(User, uid)
    if target is None:
        return _redirect("/admin/users", "账号不存在", "err")
    if role not in auth.ROLE_PERMISSIONS:
        return _redirect("/admin/users", "角色不合法", "err")
    if target.id == actor.id and role != "admin":
        return _redirect("/admin/users", "不能取消自己的管理员权限（避免锁死）", "err")
    if target.role == "admin" and role != "admin" and _active_admin_count(session) <= 1:
        return _redirect("/admin/users", "系统至少需要保留一名启用状态的管理员", "err")

    target.role = role
    session.add(target)
    session.commit()
    if role != "admin":
        auth.revoke_user_sessions(session, target.id)  # type: ignore[arg-type]
    return _redirect("/admin/users", f"{target.username} 的角色已改为 {auth.ROLE_LABEL[role]}")


@app.post("/admin/users/{uid}/toggle")
def admin_toggle_user(
    uid: int,
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_USERS)),
    session: Session = Depends(get_session),
):
    target = session.get(User, uid)
    if target is None:
        return _redirect("/admin/users", "账号不存在", "err")
    if target.id == actor.id:
        return _redirect("/admin/users", "不能停用自己的账号", "err")
    if target.role == "admin" and target.is_active and _active_admin_count(session) <= 1:
        return _redirect("/admin/users", "系统至少需要保留一名启用状态的管理员", "err")

    target.is_active = not target.is_active
    session.add(target)
    session.commit()
    if not target.is_active:
        auth.revoke_user_sessions(session, target.id)  # type: ignore[arg-type]
    state = "启用" if target.is_active else "停用"
    return _redirect("/admin/users", f"{target.username} 已{state}")


@app.post("/admin/users/{uid}/password")
def admin_reset_password(
    uid: int,
    password: str = Form(...),
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_USERS)),
    session: Session = Depends(get_session),
):
    target = session.get(User, uid)
    if target is None:
        return _redirect("/admin/users", "账号不存在", "err")
    problem = auth.check_password_policy(password)
    if problem:
        return _redirect("/admin/users", problem, "err")

    target.password_hash = auth.hash_password(password)
    session.add(target)
    session.commit()
    auth.revoke_user_sessions(session, target.id)  # type: ignore[arg-type]
    return _redirect("/admin/users", f"{target.username} 的密码已重置，其会话已全部失效")


# ── 管理员：系统参数 ───────────────────────────────────────────────────────
@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_SETTINGS)),
    session: Session = Depends(get_session),
    ok: str | None = Query(default=None),
    err: str | None = Query(default=None),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    return templates.TemplateResponse(
        request=request,
        name="admin_settings.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="系统设置",
            page_sub="调整运行参数，保存后立即生效",
            active="settings",
            items=settings_store.snapshot(session),
            groups=settings_store.GROUPS,
            ok=ok,
            err=err,
        ),
    )


@app.post("/admin/settings")
async def admin_settings_save(
    request: Request,
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_SETTINGS)),
    session: Session = Depends(get_session),
):
    form = await request.form()
    values = {k: v for k, v in form.items() if k in settings_store.SPEC_INDEX}
    if not values:
        return _redirect("/admin/settings", "没有提交任何配置项", "err")

    changed, errors = settings_store.update_many(session, values, actor.username)
    if errors:
        return _redirect("/admin/settings", "；".join(errors), "err")
    return _redirect("/admin/settings", f"已保存 {changed} 项配置")


# ── 健康检查（容器用，不过鉴权）───────────────────────────────────────────
@app.get("/healthz")
def healthz():
    return {"ok": True}
