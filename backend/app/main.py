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
from .compare import build_matrix, completeness, diff_count, format_number, metrics_for_categories
from .datasheet import extract_candidates, read_document, summarize
from .db import engine, get_session, init_db
from .exporters import matrix_xlsx
from .importer import (
    CSV_TEMPLATE,
    import_csv,
    import_rows,
    import_xlsx,
    rows_from_csv,
    rows_from_xlsx,
    xlsx_template,
)
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


app = FastAPI(title="竞品情报看板", version="0.5.0", lifespan=lifespan)
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
    if path.startswith("/api/") or path.startswith("/export.") or path.endswith("/file"):
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


def _norm_company(name: str) -> str:
    """公司名归一：去掉空白与标点、统一大小写，用于判断是否同一家。"""
    return re.sub(r"[\s\-_.·/\\()（）&]+", "", name or "").casefold()


def _find_company(session: Session, name: str) -> tuple[Entity | None, list[Entity]]:
    """返回 (完全同名, 近名列表)。

    近名用于拦「村田」与「村田制作所」建成两份档案 —— 名称归一的前提是先别造出重复。
    """
    key = _norm_company(name)
    exact: Entity | None = None
    similar: list[Entity] = []
    if not key:
        return None, []
    for e in session.exec(select(Entity)).all():
        other = _norm_company(e.name)
        if not other:
            continue
        if other == key:
            exact = e
        elif len(key) >= 2 and (key in other or other in key):
            similar.append(e)
    return exact, similar


def _find_product(session: Session, entity_id: int, model: str) -> Product | None:
    """同公司内按型号查（忽略大小写与首尾空白）。"""
    target = (model or "").strip().casefold()
    for p in session.exec(select(Product).where(Product.entity_id == entity_id)).all():
        if (p.model or "").strip().casefold() == target:
            return p
    return None


def _validate_target(category: str, model: str) -> str:
    if category not in config.CATEGORY_LABEL:
        raise HTTPException(400, f"未知产品大类「{category}」，可选：{'、'.join(config.CATEGORY_LABEL.values())}")
    model = (model or "").strip()
    if not model:
        raise HTTPException(400, "请填写规格书对应的型号")
    if len(model) > 64:
        raise HTTPException(400, "型号过长（≤ 64 字符）")
    return model


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
    entity_id: int | None = Query(default=None),
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    entities, products, _, _ = _load(session)

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
            metric_total=len(products),
            product_total=len(products),
            min_confidence=sset["datasheet.min_confidence"],
            max_upload_mb=sset["datasheet.max_upload_mb"],
        ),
    )


@app.post("/api/datasheet/analyze")
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
            raise HTTPException(400, "客户不是竞品来源，请选择友商或本方")
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


@app.post("/api/datasheet/commit")
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
            raise HTTPException(400, "客户不是竞品来源，请选择友商或本方")
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
        "entity": entity.name,
        "entity_created": entity_created,
        "product": product.model,
        "product_created": product_created,
        "category_label": category_label(product.category),
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
SHEET_EXT = {".xlsx", ".xlsm", ".csv", ".txt"}


@app.get("/import", response_class=HTMLResponse)
def import_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
    ok: str | None = Query(default=None),
    err: str | None = Query(default=None),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    entities, products, metrics, _ = _load(session)
    ent_by_id = {e.id: e for e in entities}
    return templates.TemplateResponse(
        request=request,
        name="import.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="批量导入",
            page_sub="用 Excel 或 CSV 批量写入参数；型号与友商不存在会自动建档",
            active="import",
            ok=ok,
            err=err,
            entities=entities,
            ent_by_id=ent_by_id,
            product_total=len(products),
            metric_total=len(metrics),
            categories=list(config.CATEGORY_LABEL.items()),
            columns=list(_import_columns()),
        ),
    )


def _import_columns() -> list[tuple[str, str, str]]:
    return [
        ("型号", "必填", "已存在则更新该型号参数；不存在则新建型号"),
        ("友商", "建议填", "公司名称；已存在则复用（忽略空格/标点/大小写）"),
        ("大类", "建议填", "只能填：" + "、".join(config.CATEGORY_LABEL.values())),
        ("指标", "必填", "指标中文名或 key，必须在指标字典中存在，否则该行跳过"),
        ("数值", "必填", "数值型填数字；文本型填原文；留空表示未公开，跳过"),
        ("单位", "可空", "仅作说明，实际单位以指标字典为准"),
        ("条件", "可空", "如 @15Vpp，会出现在溯源浮层"),
        ("来源链接", "可空", "越具体越好，便于逐项复核"),
        ("置信度", "可空", "0~1，缺省 1.0"),
    ]


@app.post("/api/import", response_model=None)
async def api_import(
    file: UploadFile,
    _csrf: None = Depends(auth.verify_csrf),
    _user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    """Excel 与 CSV 走同一套校验，只在「读文件」这一步分叉。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SHEET_EXT:
        raise HTTPException(400, f"暂不支持 {ext or '未知'} 格式，请上传 .xlsx / .csv")

    note = f"{file.filename or '表格导入'}"
    if ext in (".xlsx", ".xlsm"):
        report = import_xlsx(session, raw, batch_note=note)
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        report = import_csv(session, text, batch_note=note)
    return report.as_dict()


@app.get("/api/import/template.xlsx")
def api_import_template_xlsx(_pair=Depends(_perm(P_WRITE))):
    return StreamingResponse(
        io.BytesIO(xlsx_template()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=spec-import-template.xlsx"},
    )


@app.get("/api/import/template")
def api_import_template(_pair=Depends(_perm(P_WRITE))):
    return StreamingResponse(
        io.BytesIO(CSV_TEMPLATE.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=spec-import-template.csv"},
    )


def _export_dataset(p: str | None, session: Session) -> dict:
    sset = settings_store.all_values(session)
    entities, products, _, _ = _load(session)
    ids = _parse_ids(
        p,
        products,
        entities,
        preferred_cat=sset["ui.default_category"],
        limit=sset["compare.max_products"],
    )
    return _matrix_payload(session, ids, sset)


@app.get("/export.xlsx")
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


@app.get("/export.csv")
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


# ── 产品管理：管理员直接改数据 ─────────────────────────────────────────────
def _product_completeness(session: Session, product: Product, metrics: list[SpecMetric]) -> tuple[int, int]:
    """返回 (已公开项, 该大类适用项)。缺失的指标不计入分子。"""
    pool = metrics_for_categories(metrics, {product.category} if product.category else set())
    ids = [m.id for m in pool if m.id is not None]
    if not ids:
        return 0, 0
    specs = session.exec(
        select(ProductSpec).where(
            ProductSpec.product_id == product.id,
            ProductSpec.metric_id.in_(ids),  # type: ignore[attr-defined]
        )
    ).all()
    return len(specs), len(pool)


@app.get("/products", response_class=HTMLResponse)
def products_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
    ok: str | None = Query(default=None),
    err: str | None = Query(default=None),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    entities, products, metrics, _ = _load(session)
    ent_by_id = {e.id: e for e in entities}

    table = []
    for p in products:
        filled, total = _product_completeness(session, p, metrics)
        table.append({
            "product": p,
            "entity": ent_by_id.get(p.entity_id),
            "filled": filled,
            "total": total,
            "pct": round(filled / total * 100) if total else 0,
        })
    table.sort(key=lambda r: ((r["entity"].name if r["entity"] else ""), r["product"].model))

    return templates.TemplateResponse(
        request=request,
        name="products.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="产品管理",
            page_sub="直接编辑型号与参数值；这里的改动按人工录入处理，来源记为 manual",
            active="products",
            ok=ok,
            err=err,
            rows=table,
            companies=[e for e in entities if e.kind in ("self", "competitor")],
            categories=list(config.CATEGORY_LABEL.items()),
            cat_all=config.CATEGORY_LABEL,
            product_total=len(products),
        ),
    )


@app.post("/products")
def product_create(
    entity_id: int = Form(...),
    model: str = Form(...),
    category: str = Form(...),
    name: str = Form(default=""),
    status: str = Form(default="active"),
    _csrf: None = Depends(auth.verify_csrf),
    _user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    entity = session.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404, "公司不存在")
    if entity.kind == "customer":
        raise HTTPException(400, "客户不是竞品来源")
    model = _validate_target(category, model)
    if _find_product(session, entity_id, model):
        return _redirect("/products", f"型号 {model} 已存在于该公司，未重复创建", "err")

    product = Product(entity_id=entity_id, model=model, name=name.strip() or model,
                      category=category, status=status or "active")
    session.add(product)
    session.commit()
    return _redirect(f"/products/{product.id}", f"已创建型号 {model}，现在可以录入参数")


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product_edit_page(
    product_id: int,
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
    ok: str | None = Query(default=None),
    err: str | None = Query(default=None),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "型号不存在")

    entities, _, metrics, src_by_id = _load(session)
    ent_by_id = {e.id: e for e in entities}
    pool = metrics_for_categories(metrics, {product.category} if product.category else set())
    specs = {
        s.metric_id: s
        for s in session.exec(
            select(ProductSpec).where(ProductSpec.product_id == product.id)
        ).all()
    }

    rows = []
    for m in pool:
        spec = specs.get(m.id)
        value = ""
        if spec:
            if m.value_type == "number" and spec.value_num is not None:
                value = format_number(spec.value_num, m.precision)
            else:
                value = spec.value_text or ""
        rows.append({
            "metric": m,
            "spec": spec,
            "value": value,
            "source": src_by_id.get(spec.source_id) if spec and spec.source_id else None,
        })

    return templates.TemplateResponse(
        request=request,
        name="product_edit.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title=product.model,
            page_sub=f"{ent_by_id[product.entity_id].name if product.entity_id in ent_by_id else ''}"
                     f" · {category_label(product.category)} · 参数 {len(specs)}/{len(pool)}",
            active="products",
            ok=ok,
            err=err,
            product=product,
            entity=ent_by_id.get(product.entity_id),
            rows=rows,
            categories=list(config.CATEGORY_LABEL.items()),
            statuses=[("active", "在售"), ("eol", "停产"), ("rumored", "传闻")],
        ),
    )


@app.post("/products/{product_id}")
def product_save_meta(
    product_id: int,
    model: str = Form(...),
    name: str = Form(default=""),
    category: str = Form(...),
    status: str = Form(default="active"),
    datasheet_url: str = Form(default=""),
    note: str = Form(default=""),
    _csrf: None = Depends(auth.verify_csrf),
    _user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "型号不存在")
    model = _validate_target(category, model)

    other = _find_product(session, product.entity_id, model)
    if other and other.id != product.id:
        return _redirect(f"/products/{product_id}", f"型号 {model} 已被本公司另一条记录占用", "err")

    if product.category and category != product.category:
        # 换大类会让历史参数落到错误的指标体系里，必须先清空或另建型号
        has_specs = session.exec(
            select(ProductSpec).where(ProductSpec.product_id == product.id)
        ).first()
        if has_specs:
            return _redirect(
                f"/products/{product_id}",
                f"该型号已有参数，不能从「{category_label(product.category)}」改成"
                f"「{category_label(category)}」；请先清空参数或另建型号",
                "err",
            )

    product.model = model
    product.name = name.strip() or model
    product.category = category
    product.status = status or "active"
    product.datasheet_url = datasheet_url.strip() or None
    product.note = note.strip() or None
    session.add(product)
    session.commit()
    return _redirect(f"/products/{product_id}", "型号信息已保存")


@app.post("/products/{product_id}/specs")
async def product_save_specs(
    product_id: int,
    request: Request,
    _csrf: None = Depends(auth.verify_csrf),
    user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "型号不存在")

    form = await request.form()
    _, _, metrics, _ = _load(session)
    pool = metrics_for_categories(metrics, {product.category} if product.category else set())
    existing = {
        s.metric_id: s
        for s in session.exec(
            select(ProductSpec).where(ProductSpec.product_id == product.id)
        ).all()
    }

    source = SpecSource(
        kind="manual",
        title=f"{product.model} 手工编辑",
        captured_at=datetime.now(timezone.utc),
        note=f"录入人 {user.username}；人工录入视为已核对，可逐项改回未复核",
    )
    session.add(source)
    session.flush()

    written = updated = removed = 0
    errors: list[str] = []
    for m in pool:
        if m.id is None:
            continue
        # 表单里根本没出现这个指标 = 本次没打算动它。
        # 早期版本把「没提交」当成「清空」，只要页面漏渲染一项（或指标字典新增了一项）
        # 就会把已有参数静默删掉 —— 必须区分「空值」与「未提交」。
        field = f"{m.id}:v"
        if field not in form:
            continue
        raw = str(form.get(field, "")).strip()
        cond = str(form.get(f"{m.id}:c", "")).strip() or None
        verified = form.get(f"{m.id}:ok") == "on"
        cur = existing.get(m.id)

        if not raw:
            # 空值 = 未公开。已存在的行删掉，不留「有行无值」的脏数据
            if cur:
                session.delete(cur)
                removed += 1
            continue

        if m.value_type == "number":
            try:
                num = float(raw.replace(",", ""))
            except ValueError:
                errors.append(f"{m.name}：要求数值，收到「{raw}」，未保存")
                continue
            value_text = None
        else:
            num, value_text = None, raw

        if cur:
            cur.value_num, cur.value_text = num, value_text
            cur.condition, cur.source_id = cond, source.id
            cur.confidence, cur.verified = 1.0, verified
            cur.updated_at = datetime.now(timezone.utc)
            session.add(cur)
            updated += 1
        else:
            session.add(ProductSpec(
                product_id=product.id, metric_id=m.id,
                value_num=num, value_text=value_text, condition=cond,
                source_id=source.id, confidence=1.0, verified=verified,
            ))
            written += 1

    session.commit()
    msg = f"已保存：新增 {written}、更新 {updated}、清除 {removed}"
    if errors:
        msg += "；" + "；".join(errors)
        return _redirect(f"/products/{product_id}", msg, "err")
    return _redirect(f"/products/{product_id}", msg)


@app.post("/products/{product_id}/delete")
def product_delete(
    product_id: int,
    _csrf: None = Depends(auth.verify_csrf),
    _user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "型号不存在")
    model = product.model
    for spec in session.exec(
        select(ProductSpec).where(ProductSpec.product_id == product.id)
    ).all():
        session.delete(spec)
    session.delete(product)
    session.commit()
    return _redirect("/products", f"已删除型号 {model}（含其全部参数）")


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
