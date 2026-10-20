"""机器调用接口（/api/v1）—— 为 AI / 脚本预留的稳定接口面。

为什么要单独一层，而不是让 AI 直接调现有页面路由：
1. 现有写入路由是**表单 + 303 跳转**，返回给人看的 HTML，程序无法判断成败；
2. 现有非幂等请求强制 **CSRF 双重提交**，而 AI 调用没有浏览器会话；
3. 导入失败会留下半成品（早期每建一条就 commit），重试会写两遍。

本层因此定了五条规矩：
- 统一信封：成功 `{ok,data,meta}`，失败 `{ok:false,error:{code,message,hint,field}}`。
  `code` 是稳定枚举，供程序分支；`message/hint` 给人看。
- 令牌鉴权：`Authorization: Bearer cib_<prefix>_<secret>`，用 scope 控权限（read/write/ingest/admin）。
  令牌不走 Cookie，所以不需要 CSRF —— 也就不存在「为了机器调用而放宽浏览器安全」的问题。
- 幂等：写接口支持 `Idempotency-Key`，重放返回首次响应，不重复写库。
- 预演：所有写接口支持 `dry_run=true`，只校验并回报将发生什么，不落库。
- 并发：资源返回 `ETag`，更新可用 `If-Match`；版本不匹配返回 409，避免覆盖别人的改动。

路由按「用途」分四组，而不是按对象分：
    查询  GET  /api/v1/{context,entities,metrics,products,matrix,changes}
    写入  POST/PATCH/DELETE /api/v1/products*, POST /api/v1/specs/batch
    摄取  POST /api/v1/import
    发现  GET  /api/v1/tools
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from . import audit, auth, config, settings_store, tokens
from .compare import format_number, metrics_for_categories
from .db import get_session
from .importer import import_rows
from .models import ApiIdempotency, Entity, Product, ProductSpec, SpecMetric
from .numeric import clamp_confidence
from .sanitize import safe_url
from .timeutil import iso_utc

router = APIRouter(prefix="/api/v1", tags=["v1"])

# 错误码表 —— 程序分支只看 code，不要解析 message
E: dict[str, int] = {
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "VALIDATION_FAILED": 422,
    "MODEL_EXISTS": 409,
    "ENTITY_EXISTS": 409,
    "ENTITY_SIMILAR": 409,
    "CATEGORY_CONFLICT": 409,
    "CONFLICT_VERSION": 409,
    "CONFIRM_REQUIRED": 400,
}


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        field: str | None = None,
        details: Any = None,
        status: int | None = None,
    ):
        self.code = code
        self.message = message
        self.hint = hint
        self.field = field
        self.details = details
        self.status = status or E.get(code, 400)
        super().__init__(message)


# ── 信封 ───────────────────────────────────────────────────────────────────
def _meta(**extra) -> dict:
    m = {"request_id": uuid.uuid4().hex[:12]}
    m.update({k: v for k, v in extra.items() if v is not None})
    return m


def ok(data: Any, *, status: int = 200, headers: dict | None = None, **meta) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "data": data, "meta": _meta(**meta)},
        status_code=status,
        headers=headers,
    )


def fail(error: ApiError) -> JSONResponse:
    body: dict[str, Any] = {
        "ok": False,
        "error": {"code": error.code, "message": error.message},
        "meta": _meta(),
    }
    for key in ("hint", "field", "details"):
        value = getattr(error, key)
        if value is not None:
            body["error"][key] = value
    return JSONResponse(body, status_code=error.status)


# ── 身份与 scope ───────────────────────────────────────────────────────────
@dataclass
class Principal:
    kind: str  # token / user
    name: str
    scopes: frozenset[str]
    user_id: int | None = None
    token_id: int | None = None


def _cookie_principal(request: Request, session: Session) -> Principal | None:
    """浏览器会话也能调 v1（方便手测），权限按角色折算成 scope。"""
    raw = request.cookies.get(auth.COOKIE_NAME, "")
    if not raw:
        return None
    hit = auth.resolve_session(session, raw)
    if hit is None:
        return None
    user, _ = hit
    perms = auth.permissions_for(user)
    scopes: set[str] = set()
    if auth.P_VIEW in perms or auth.P_EXPORT in perms:
        scopes.add("read")
    if auth.P_WRITE in perms:
        scopes |= {"write", "ingest"}
    if auth.P_USERS in perms or auth.P_SETTINGS in perms:
        scopes.add("admin")
    return Principal("user", user.username, frozenset(scopes), user_id=user.id)


def get_principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        row = tokens.verify(session, header.split(" ", 1)[1])
        if row is None:
            raise ApiError(
                "UNAUTHENTICATED",
                "令牌无效、已撤销或已过期",
                hint="在「API 令牌」页重新签发；确认 Bearer 后没有多余空格",
            )
        tokens.touch(session, row)
        return Principal("token", row.name, tokens.scopes_of(row), token_id=row.id)

    principal = _cookie_principal(request, session)
    if principal is None:
        raise ApiError(
            "UNAUTHENTICATED",
            "缺少凭证",
            hint="机器调用请带 Authorization: Bearer <token>；浏览器调试请先登录",
        )
    return principal


def require_scope(scope: str):
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if scope not in principal.scopes and "admin" not in principal.scopes:
            raise ApiError(
                "FORBIDDEN",
                f"缺少 {scope} 权限",
                hint=f"当前 scope：{', '.join(sorted(principal.scopes)) or '无'}",
            )
        return principal

    return dependency


# ── 序列化 ─────────────────────────────────────────────────────────────────
def _entity_dict(e: Entity) -> dict:
    return {
        "id": e.id, "kind": e.kind, "name": e.name, "name_en": e.name_en,
        "region": e.region, "tier": e.tier,
    }


def _metric_dict(m: SpecMetric) -> dict:
    return {
        "key": m.key, "name": m.name, "unit": m.unit, "value_type": m.value_type,
        "better": m.better, "precision": m.precision,
        "applies_to": [s for s in (m.applies_to or "").split(",") if s],
        "group": m.category,
    }


def _product_dict(p: Product, ent: Entity | None, filled: int | None = None,
                 total: int | None = None) -> dict:
    out = {
        "id": p.id, "model": p.model, "name": p.name, "category": p.category,
        "category_label": config.category_label(p.category), "status": p.status,
        "entity": {
            "id": p.entity_id,
            "name": ent.name if ent else None,
            "kind": ent.kind if ent else None,
        },
        "datasheet_url": p.datasheet_url,
    }
    if filled is not None and total is not None:
        out["specs_filled"] = filled
        out["specs_total"] = total
        out["completeness"] = round(filled / total, 4) if total else 0.0
    return out


def _spec_dict(s: ProductSpec, m: SpecMetric) -> dict:
    return {
        "metric_key": m.key, "metric_name": m.name, "unit": m.unit,
        "value_num": s.value_num, "value_text": s.value_text,
        "display": format_number(s.value_num, m.precision) if s.value_num is not None else s.value_text,
        "condition": s.condition, "verified": s.verified, "confidence": s.confidence,
        "source_id": s.source_id,
        "updated_at": iso_utc(s.updated_at),
    }


def _actor(principal: Principal) -> tuple[str, str]:
    return principal.name, ("token" if principal.kind == "token" else "user")


def _etag(p: Product, specs: list[ProductSpec]) -> str:
    raw = "|".join([
        str(p.id), p.model or "", p.name or "", p.category or "", p.status or "",
        p.datasheet_url or "", p.note or "", p.launch_date.isoformat() if p.launch_date else "",
        *[
            f"{s.metric_id}:{s.value_num}:{s.value_text}:{s.condition}:{s.verified}"
            for s in sorted(specs, key=lambda x: x.metric_id or 0)
        ],
    ])
    return 'W/"' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] + '"'


def _specs_of(session: Session, product_id: int) -> list[ProductSpec]:
    return list(session.exec(
        select(ProductSpec).where(ProductSpec.product_id == product_id)
    ).all())


def _filled_counts(session: Session) -> dict[int, int]:
    rows = session.exec(
        select(ProductSpec.product_id, func.count()).group_by(ProductSpec.product_id)  # type: ignore[arg-type]
    ).all()
    return {int(pid): int(n) for pid, n in rows}


# ── 校验 ───────────────────────────────────────────────────────────────────
def _check_category(category: str | None) -> str:
    if not category or category not in config.CATEGORY_LABEL:
        raise ApiError(
            "VALIDATION_FAILED",
            f"未知产品大类「{category or ''}」",
            field="category",
            hint="可选：" + "、".join(f"{c}（{label}）" for c, label in config.CATEGORY_LABEL.items()),
        )
    return category


PRODUCT_STATUSES = ("active", "eol", "rumored")


def _check_status(status: str | None) -> str:
    value = (status or "active").strip()
    if value not in PRODUCT_STATUSES:
        raise ApiError(
            "VALIDATION_FAILED",
            f"status 只能是 {' / '.join(PRODUCT_STATUSES)}",
            field="status",
        )
    return value


def _clean_model(model: str | None) -> str:
    value = (model or "").strip()
    if not value:
        raise ApiError("VALIDATION_FAILED", "型号不能为空", field="model")
    if len(value) > 64:
        raise ApiError("VALIDATION_FAILED", "型号过长（≤64 字符）", field="model")
    return value


def _norm_name(name: str) -> str:
    import re

    return re.sub(r"[\s\-_.·/\\()（）&]+", "", name or "").casefold()


def _resolve_entity_for_create(session: Session, entity_id: int | None,
                               entity_name: str | None, force: bool) -> tuple[Entity, bool]:
    """返回 (公司, 是否本次新建)。

    「命中同名就复用」对建型号是正确的（型号必须挂在已有公司下），但对
    `POST /entities` 就会误导：调用方拿到 201 以为建成了，审计里还多一条
    「新建公司」的假记录。所以要连「是不是真的新建了」一起返回。
    """
    if entity_id is not None:
        ent = session.get(Entity, entity_id)
        if ent is None:
            raise ApiError("NOT_FOUND", f"公司 id={entity_id} 不存在", field="entity_id")
        if ent.kind == "customer":
            raise ApiError("VALIDATION_FAILED", "客户不是竞品来源", field="entity_id")
        return ent, False

    name = (entity_name or "").strip()
    if not name:
        raise ApiError(
            "VALIDATION_FAILED", "需要 entity_id 或 entity_name",
            hint="已有公司传 entity_id；新公司传 entity_name",
        )
    key = _norm_name(name)
    similar: list[Entity] = []
    for e in session.exec(select(Entity)).all():
        other = _norm_name(e.name)
        if not other:
            continue
        if other == key:
            return e, False
        if len(key) >= 2 and (key in other or other in key):
            similar.append(e)
    if similar and not force:
        raise ApiError(
            "ENTITY_SIMILAR",
            f"已存在相似公司「{similar[0].name}」",
            field="entity_name",
            hint="改用它的 entity_id；若确实是另一家，传 force=true",
            details=[_entity_dict(e) for e in similar[:5]],
        )
    ent = Entity(kind="competitor", name=name, tier="C")
    session.add(ent)
    session.flush()
    return ent, True


def _find_product_ci(session: Session, entity_id: int, model: str) -> Product | None:
    target = model.strip().casefold()
    for p in session.exec(select(Product).where(Product.entity_id == entity_id)).all():
        if (p.model or "").strip().casefold() == target:
            return p
    return None


# ── 幂等 ───────────────────────────────────────────────────────────────────
def _idem_key(principal: Principal, header: str | None) -> str | None:
    if not header:
        return None
    owner = principal.token_id or principal.user_id or 0
    return hashlib.sha256(f"{owner}:{header}".encode("utf-8")).hexdigest()


def _replay(session: Session, key: str | None, method: str, path: str) -> JSONResponse | None:
    if not key:
        return None
    row = session.get(ApiIdempotency, key)
    if row is None or row.method != method or row.path != path:
        return None
    body = json.loads(row.response_json)
    body.setdefault("meta", {})["replayed"] = True
    return JSONResponse(body, status_code=row.status_code)


def _remember(session: Session, key: str | None, method: str, path: str,
              response: JSONResponse) -> None:
    if not key:
        return
    session.add(ApiIdempotency(
        key=key, method=method, path=path,
        status_code=response.status_code,
        response_json=response.body.decode("utf-8"),
    ))
    session.commit()


def _is_dry(request: Request, body: BaseModel | None = None) -> bool:
    if request.query_params.get("dry_run", "").lower() in ("1", "true", "yes"):
        return True
    return bool(getattr(body, "dry_run", False))


# ── 请求体 ─────────────────────────────────────────────────────────────────
class EntityIn(BaseModel):
    name: str
    kind: str = "competitor"
    name_en: str | None = None
    region: str | None = None
    tier: str = "C"
    force: bool = False
    dry_run: bool = False


class ProductIn(BaseModel):
    entity_id: int | None = None
    entity_name: str | None = None
    model: str
    category: str
    name: str | None = None
    status: str = "active"
    force: bool = False
    dry_run: bool = False


class ProductPatch(BaseModel):
    model: str | None = None
    name: str | None = None
    category: str | None = None
    status: str | None = None
    datasheet_url: str | None = None
    note: str | None = None
    dry_run: bool = False


class SpecItem(BaseModel):
    product_id: int
    metric_key: str
    action: str = "upsert"  # upsert / delete
    value_num: float | None = None
    value_text: str | None = None
    condition: str | None = None
    verified: bool = False
    confidence: float | None = None


class SpecBatch(BaseModel):
    items: list[SpecItem]
    dry_run: bool = False


class ImportIn(BaseModel):
    rows: list[dict]
    note: str | None = None
    dry_run: bool = False


class TokenIn(BaseModel):
    name: str
    scopes: list[str] = ["read"]
    expires_days: int | None = None


# ── 查询面 ─────────────────────────────────────────────────────────────────
@router.get("/health")
def health():
    return ok({"status": "ok", "version": "v1"})


@router.get("/context")
def get_context(
    _p: Principal = Depends(require_scope("read")),
    session: Session = Depends(get_session),
):
    """给模型「先看一眼全局」的入口。逐个查询代价高，这个一次拿全骨架。"""
    entities = session.exec(select(Entity)).all()
    products = session.exec(select(Product)).all()
    metrics = session.exec(select(SpecMetric).order_by(SpecMetric.display_order)).all()
    spec_count = session.exec(select(func.count()).select_from(ProductSpec)).one()
    name_by_id = {e.id: e.name for e in entities}
    return ok({
        "counts": {
            "entities": len(entities), "products": len(products),
            "metrics": len(metrics), "specs": int(spec_count or 0),
        },
        "categories": [{"code": c, "label": label} for c, label in config.CATEGORY_LABEL.items()],
        "entities": [_entity_dict(e) for e in entities],
        "products": [
            {"id": p.id, "model": p.model, "category": p.category,
             "entity": name_by_id.get(p.entity_id)}
            for p in products
        ],
        "metric_keys": [
            {"key": m.key, "name": m.name, "unit": m.unit, "applies_to": m.applies_to}
            for m in metrics
        ],
        "hint": "写参数时 metric_key 请用 key，不要用中文名 —— 名称可能改，key 不会。",
    })


@router.get("/metrics")
def list_metrics(
    category: str | None = Query(default=None, description="只看某大类适用的指标"),
    _p: Principal = Depends(require_scope("read")),
    session: Session = Depends(get_session),
):
    metrics = list(session.exec(select(SpecMetric).order_by(SpecMetric.display_order)).all())
    if category:
        _check_category(category)
        metrics = metrics_for_categories(metrics, {category})
    return ok([_metric_dict(m) for m in metrics], total=len(metrics))


@router.get("/entities")
def list_entities(
    kind: str | None = Query(default=None, description="self / competitor / customer"),
    _p: Principal = Depends(require_scope("read")),
    session: Session = Depends(get_session),
):
    rows = list(session.exec(select(Entity).order_by(Entity.id)).all())
    if kind:
        rows = [e for e in rows if e.kind == kind]
    return ok([_entity_dict(e) for e in rows], total=len(rows))


@router.get("/products")
def list_products(
    entity_id: int | None = None,
    category: str | None = None,
    status: str | None = None,
    q: str | None = Query(default=None, description="型号模糊搜索"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: int | None = Query(default=None, description="上一页最后一条的 id"),
    _p: Principal = Depends(require_scope("read")),
    session: Session = Depends(get_session),
):
    stmt = select(Product)
    if entity_id is not None:
        stmt = stmt.where(Product.entity_id == entity_id)
    if category:
        stmt = stmt.where(Product.category == _check_category(category))
    if status:
        stmt = stmt.where(Product.status == status)
    if q:
        stmt = stmt.where(Product.model.ilike(f"%{q}%"))  # type: ignore[attr-defined]
    if cursor is not None:
        stmt = stmt.where(Product.id > cursor)  # type: ignore[operator]

    rows = list(session.exec(stmt.order_by(Product.id).limit(limit + 1)).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    filled = _filled_counts(session)
    metrics = list(session.exec(select(SpecMetric)).all())
    ents = {e.id: e for e in session.exec(select(Entity)).all()}

    data = []
    for p in rows:
        pool = metrics_for_categories(metrics, {p.category} if p.category else set())
        data.append(_product_dict(p, ents.get(p.entity_id), filled.get(p.id or 0, 0), len(pool)))
    # total 曾是「本页条数」，调用方据此以为库里只有 3 条。分开说清楚：
    # count = 本页条数，has_more + next_cursor = 还有没有下一页。
    return ok(
        data,
        count=len(data),
        has_more=has_more,
        next_cursor=rows[-1].id if has_more and rows else None,
    )


@router.get("/products/{product_id}")
def get_product(
    product_id: int,
    _p: Principal = Depends(require_scope("read")),
    session: Session = Depends(get_session),
):
    product = session.get(Product, product_id)
    if product is None:
        raise ApiError("NOT_FOUND", f"型号 id={product_id} 不存在")
    specs = _specs_of(session, product_id)
    metrics = {m.id: m for m in session.exec(select(SpecMetric)).all()}
    pool = metrics_for_categories(list(metrics.values()),
                                  {product.category} if product.category else set())
    ent = session.get(Entity, product.entity_id)

    present = {metrics[s.metric_id].key for s in specs if s.metric_id in metrics}
    payload = _product_dict(product, ent, len(specs), len(pool))
    payload["specs"] = [
        _spec_dict(s, metrics[s.metric_id]) for s in specs if s.metric_id in metrics
    ]
    payload["missing_metrics"] = [m.key for m in pool if m.key not in present]
    etag = _etag(product, specs)          # 原来算了两遍
    return ok(payload, etag=etag, headers={"ETag": etag})


@router.get("/products/{product_id}/specs")
def get_specs(
    product_id: int,
    _p: Principal = Depends(require_scope("read")),
    session: Session = Depends(get_session),
):
    if session.get(Product, product_id) is None:
        raise ApiError("NOT_FOUND", f"型号 id={product_id} 不存在")
    metrics = {m.id: m for m in session.exec(select(SpecMetric)).all()}
    specs = _specs_of(session, product_id)
    return ok([_spec_dict(s, metrics[s.metric_id]) for s in specs if s.metric_id in metrics])


@router.get("/matrix")
def get_matrix(
    p: str | None = Query(default=None, description="型号 id，逗号分隔；省略则按默认选择"),
    _principal: Principal = Depends(require_scope("read")),
    session: Session = Depends(get_session),
):
    """复用页面那套矩阵计算，保证「AI 看到的」与「人看到的」是同一份数据。"""
    from .main import _matrix_payload, _parse_ids  # 局部导入：避免与 main 循环依赖

    products = list(session.exec(select(Product)).all())
    sset = settings_store.all_values(session)
    ids = _parse_ids(
        p, products,
        preferred_cat=sset["ui.default_category"],
        limit=sset["compare.max_products"],
    )
    data = _matrix_payload(session, ids, sset)
    return ok({
        "products": [
            {"id": pr.id, "model": pr.model, "category": pr.category,
             "entity": data["ent_by_id"][pr.entity_id].name if pr.entity_id in data["ent_by_id"] else None,
             "completeness": data["completeness"].get(pr.id, 0)}
            for pr in data["products"]
        ],
        "rows": [
            {
                "metric_key": r.metric.key, "metric": r.metric.name, "unit": r.metric.unit,
                "better": r.metric.better, "differs": r.differs, "has_missing": r.has_missing,
                "values": {
                    str(pid): {
                        "display": cell.display, "missing": cell.is_missing,
                        "best": pid in r.best, "verified": cell.verified,
                        "source_id": cell.source_id,
                    }
                    for pid, cell in r.cells.items()
                },
            }
            for r in data["rows"]
        ],
        "diff_total": data["diff_total"],
    })


@router.get("/changes")
def get_changes(
    since: str = Query(description="ISO 时间，如 2026-10-20T08:00:00Z"),
    limit: int = Query(default=200, ge=1, le=1000),
    _p: Principal = Depends(require_scope("read")),
    session: Session = Depends(get_session),
):
    """增量同步：给 AI 一次只拉「上次之后变过的东西」，不必全量重读。"""
    try:
        ts = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(
            "VALIDATION_FAILED", "since 不是合法 ISO 时间",
            field="since", hint="示例：2026-10-20T08:00:00Z",
        ) from None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    specs = list(session.exec(
        select(ProductSpec)
        .where(ProductSpec.updated_at >= ts)
        .order_by(ProductSpec.updated_at, ProductSpec.id)
        .limit(limit)          # 下推：原来先全量 .all() 再切片，数据一多就是白读
    ).all())
    metrics = {m.id: m for m in session.exec(select(SpecMetric)).all()}
    products = {p.id: p for p in session.exec(select(Product)).all()}

    items = []
    for s in specs:
        p = products.get(s.product_id)
        if p is None:
            continue
        items.append({
            "product_id": p.id, "model": p.model, "category": p.category,
            "spec": _spec_dict(s, metrics[s.metric_id]) if s.metric_id in metrics else None,
        })
    next_since = (iso_utc(specs[-1].updated_at) or since) if specs else since
    return ok(items, total=len(items), next_since=next_since,
              hint="把 next_since 原样作为下次的 since；产品元信息变更不在本接口范围内")


# ── 写入面 ─────────────────────────────────────────────────────────────────
@router.post("/entities")
def create_entity(
    body: EntityIn,
    request: Request,
    principal: Principal = Depends(require_scope("write")),
    session: Session = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _idem_key(principal, idempotency_key)
    if (replayed := _replay(session, key, "POST", "/api/v1/entities")) is not None:
        return replayed

    ent, created = _resolve_entity_for_create(session, None, body.name, body.force)
    dry = _is_dry(request, body)
    data = _entity_dict(ent)
    if dry:
        session.rollback()
        resp = ok(data, dry_run=True, created=created, hint="预演：未落库")
    elif not created:
        # 同名公司已存在：不是错误，但也不能报 201「已创建」并记一条假的新建日志
        session.rollback()
        resp = ok(
            data, created=False,
            hint="同名公司已存在，直接复用；要改名称请用已有 id 建型号",
        )
    else:
        actor, kind = _actor(principal)
        audit.record(
            session, actor=actor, actor_kind=kind, action="create", object_type="entity",
            object_id=ent.id, label=ent.name, origin="api",
            changes=[{"field": "公司", "old": None, "new": ent.name}],
            request_id=uuid.uuid4().hex[:12],
        )
        session.commit()
        resp = ok(data, status=201, created=True)
    if not dry and created:
        _remember(session, key, "POST", "/api/v1/entities", resp)
    return resp


@router.post("/products")
def create_product(
    body: ProductIn,
    request: Request,
    principal: Principal = Depends(require_scope("write")),
    session: Session = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _idem_key(principal, idempotency_key)
    if (replayed := _replay(session, key, "POST", "/api/v1/products")) is not None:
        return replayed

    category = _check_category(body.category)
    model = _clean_model(body.model)
    status = _check_status(body.status)
    ent, _created = _resolve_entity_for_create(session, body.entity_id, body.entity_name, body.force)

    existing = _find_product_ci(session, ent.id, model)  # type: ignore[arg-type]
    if existing is not None:
        raise ApiError(
            "MODEL_EXISTS",
            f"{ent.name} 下已存在型号 {existing.model}",
            field="model",
            hint=f"用 PATCH /api/v1/products/{existing.id} 更新，或用 /api/v1/specs/batch 写参数",
            details={"product_id": existing.id},
        )

    product = Product(
        entity_id=ent.id,  # type: ignore[arg-type]
        model=model, name=(body.name or model).strip(), category=category,
        status=status,
    )
    session.add(product)
    session.flush()

    dry = _is_dry(request, body)
    data = _product_dict(product, ent, 0, len(metrics_for_categories(
        list(session.exec(select(SpecMetric)).all()), {category})))
    if dry:
        session.rollback()
        resp = ok(data, dry_run=True, hint="预演：未落库")
    else:
        actor, kind = _actor(principal)
        audit.record(
            session, actor=actor, actor_kind=kind, action="create", object_type="product",
            object_id=product.id, label=product.model, origin="api",
            changes=audit.diff({}, audit.product_snapshot(product), audit.PRODUCT_LABELS),
            request_id=uuid.uuid4().hex[:12],
        )
        session.commit()
        resp = ok(data, status=201)
    if not dry:
        _remember(session, key, "POST", "/api/v1/products", resp)
    return resp


@router.patch("/products/{product_id}")
def patch_product(
    product_id: int,
    body: ProductPatch,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _p: Principal = Depends(require_scope("write")),
    session: Session = Depends(get_session),
):
    product = session.get(Product, product_id)
    if product is None:
        raise ApiError("NOT_FOUND", f"型号 id={product_id} 不存在")
    before_meta = audit.product_snapshot(product)

    specs = _specs_of(session, product_id)
    current_etag = _etag(product, specs)
    if if_match and if_match.strip() != current_etag:
        raise ApiError(
            "CONFLICT_VERSION",
            "资源已被他人修改",
            hint="重新 GET 取最新 ETag 后再改；不要用旧数据覆盖新数据",
            details={"current_etag": current_etag},
        )

    if body.model is not None:
        model = _clean_model(body.model)
        other = _find_product_ci(session, product.entity_id, model)
        if other is not None and other.id != product.id:
            raise ApiError("MODEL_EXISTS", f"型号 {model} 已被同公司另一条记录占用", field="model")
        product.model = model
    if body.name is not None:
        product.name = body.name.strip() or product.model
    if body.status is not None:
        product.status = _check_status(body.status)
    if body.datasheet_url is not None:
        product.datasheet_url = safe_url(body.datasheet_url)
    if body.note is not None:
        product.note = body.note.strip() or None
    if body.category is not None and body.category != product.category:
        category = _check_category(body.category)
        if specs:
            raise ApiError(
                "CATEGORY_CONFLICT",
                f"该型号已有 {len(specs)} 条参数，不能更换大类",
                field="category",
                hint="先清空参数，或另建一个型号",
            )
        product.category = category

    session.add(product)
    dry = _is_dry(request, body)
    data = _product_dict(product, session.get(Entity, product.entity_id))
    if dry:
        session.rollback()
        return ok(data, dry_run=True, hint="预演：未落库")
    actor, kind = _actor(_p)
    audit.record(
        session, actor=actor, actor_kind=kind, action="update", object_type="product",
        object_id=product.id, label=product.model, origin="api",
        changes=audit.diff(before_meta, audit.product_snapshot(product), audit.PRODUCT_LABELS),
        request_id=uuid.uuid4().hex[:12],
    )
    session.commit()
    return ok(data, headers={"ETag": _etag(product, _specs_of(session, product_id))})


@router.delete("/products/{product_id}")
def delete_product(
    product_id: int,
    confirm: bool = Query(default=False, description="必须显式传 true"),
    _p: Principal = Depends(require_scope("write")),
    session: Session = Depends(get_session),
):
    product = session.get(Product, product_id)
    if product is None:
        raise ApiError("NOT_FOUND", f"型号 id={product_id} 不存在")
    if not confirm:
        raise ApiError(
            "CONFIRM_REQUIRED", "删除不可撤销，需要 confirm=true",
            hint=f"将删除型号 {product.model} 及其全部参数",
        )
    specs = _specs_of(session, product_id)
    snapshot = audit.spec_snapshot(session, product_id)
    actor, kind = _actor(_p)
    audit.record(
        session, actor=actor, actor_kind=kind, action="delete", object_type="product",
        object_id=product_id, label=product.model, origin="api",
        changes=[{"field": k, "old": v, "new": None} for k, v in snapshot.items()],
        note=f"删除型号及其 {len(specs)} 项参数", request_id=uuid.uuid4().hex[:12],
    )
    for s in specs:
        session.delete(s)
    session.flush()  # 无 relationship 时，先删子表再删父表，避免外键报错
    session.delete(product)
    session.commit()
    return ok({"deleted_product_id": product_id, "deleted_specs": len(specs)})


@router.post("/specs/batch")
def batch_specs(
    body: SpecBatch,
    request: Request,
    principal: Principal = Depends(require_scope("write")),
    session: Session = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """批量写参数。逐项返回状态 —— 部分成功必须显式，不能让调用方以为全成了。"""
    key = _idem_key(principal, idempotency_key)
    if (replayed := _replay(session, key, "POST", "/api/v1/specs/batch")) is not None:
        return replayed
    if not body.items:
        raise ApiError("VALIDATION_FAILED", "items 不能为空", field="items")

    metrics = {m.key: m for m in session.exec(select(SpecMetric)).all()}
    metric_by_id = {m.id: m for m in metrics.values()}
    touched = {item.product_id for item in body.items}

    # 一次性取齐，别在循环里逐项查：50 项原本要 174 条 SQL（实测）
    products_by_id: dict[int, Product] = {
        p.id: p for p in session.exec(
            select(Product).where(Product.id.in_(touched))  # type: ignore[attr-defined]
        ).all()
    }
    specs_by_key: dict[tuple[int, int], ProductSpec] = {
        (s.product_id, s.metric_id): s for s in session.exec(
            select(ProductSpec).where(ProductSpec.product_id.in_(touched))  # type: ignore[attr-defined]
        ).all()
    }
    specs_by_pid: dict[int, list[ProductSpec]] = {}
    for s in specs_by_key.values():
        specs_by_pid.setdefault(s.product_id, []).append(s)
    before_by_pid = {
        pid: audit.spec_snapshot(session, pid, metric_by_id, specs_by_pid.get(pid, []))
        for pid in touched
    }
    results: list[dict] = []
    applied = 0
    dry = _is_dry(request, body)

    for idx, item in enumerate(body.items):
        base = {"index": idx, "product_id": item.product_id, "metric_key": item.metric_key}
        product = products_by_id.get(item.product_id)
        if product is None:
            results.append({**base, "ok": False, "code": "NOT_FOUND", "message": "型号不存在"})
            continue
        metric = metrics.get(item.metric_key)
        if metric is None:
            results.append({
                **base, "ok": False, "code": "VALIDATION_FAILED",
                "message": f"指标 {item.metric_key} 不在字典中",
                "hint": "GET /api/v1/metrics 取可用 key",
            })
            continue
        scope = {s.strip() for s in (metric.applies_to or "").split(",") if s.strip()}
        if scope and product.category and product.category not in scope:
            results.append({
                **base, "ok": False, "code": "CATEGORY_CONFLICT",
                "message": f"{metric.key} 不适用于 {config.category_label(product.category)}",
            })
            continue

        existing = specs_by_key.get((product.id, metric.id))  # type: ignore[arg-type]

        if item.action == "delete":
            if existing is None:
                results.append({**base, "ok": True, "action": "noop", "message": "原本就没有该项"})
            else:
                session.delete(existing)
                specs_by_key.pop((product.id, metric.id), None)  # type: ignore[arg-type]
                applied += 1
                results.append({**base, "ok": True, "action": "deleted"})
            continue

        if item.action != "upsert":
            results.append({
                **base, "ok": False, "code": "VALIDATION_FAILED",
                "message": "action 只能是 upsert 或 delete",
            })
            continue

        num, text = item.value_num, item.value_text
        if metric.value_type == "number":
            if num is None:
                results.append({
                    **base, "ok": False, "code": "VALIDATION_FAILED",
                    "message": f"{metric.name} 需要数值，请传 value_num",
                })
                continue
            text = None
        else:
            if not (text or "").strip():
                results.append({
                    **base, "ok": False, "code": "VALIDATION_FAILED",
                    "message": f"{metric.name} 需要文本，请传 value_text",
                })
                continue
            num = None

        if existing:
            existing.value_num, existing.value_text = num, text
            existing.condition = item.condition
            existing.verified = item.verified
            existing.confidence = clamp_confidence(item.confidence)
            existing.updated_at = datetime.now(timezone.utc)
            session.add(existing)
            action = "updated"
        else:
            session.add(ProductSpec(
                product_id=product.id, metric_id=metric.id,
                value_num=num, value_text=text, condition=item.condition,
                confidence=clamp_confidence(item.confidence),
                verified=item.verified,
            ))
            action = "created"
        applied += 1
        results.append({**base, "ok": True, "action": action})

    if dry:
        session.rollback()
        resp = ok({"results": results, "applied": applied}, dry_run=True,
                  hint="预演：未落库，results 为「将会发生」的结果")
    else:
        actor, kind = _actor(principal)
        rid = uuid.uuid4().hex[:12]
        session.flush()   # 新增项要先落库，否则「改后」快照看不到它们
        for pid in touched:
            product = products_by_id.get(pid)
            if product is None:
                continue
            changes = audit.diff(before_by_pid.get(pid, {}),
                                 audit.spec_snapshot(session, pid, metric_by_id))
            audit.record(
                session, actor=actor, actor_kind=kind, action="update", object_type="spec",
                object_id=pid, label=product.model, origin="api",
                changes=changes, note=f"批量写入 {applied} 项", request_id=rid,
            )
        session.commit()
        resp = ok({"results": results, "applied": applied},
                  failed=sum(1 for r in results if not r.get("ok")))
    if not dry:
        _remember(session, key, "POST", "/api/v1/specs/batch", resp)
    return resp


@router.post("/import")
def api_import_rows(
    body: ImportIn,
    request: Request,
    _p: Principal = Depends(require_scope("ingest")),
    session: Session = Depends(get_session),
):
    """与页面上的 Excel/CSV 导入共用同一套校验，只是输入换成 JSON 行。"""
    if not body.rows:
        raise ApiError("VALIDATION_FAILED", "rows 不能为空", field="rows")
    headers: list[str] = []
    for row in body.rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    dry = _is_dry(request, body)
    report = import_rows(
        session, body.rows, headers,
        batch_note=body.note or "API 导入",
        commit=not dry,
    )
    if not dry:
        actor, kind = _actor(_p)
        audit.log_import(session, report, actor=actor, actor_kind=kind, origin="api",
                         request_id=uuid.uuid4().hex[:12])
    return ok(report.as_dict(), dry_run=dry)


# ── 令牌管理 ───────────────────────────────────────────────────────────────
@router.get("/tokens")
def list_tokens(
    _p: Principal = Depends(require_scope("admin")),
    session: Session = Depends(get_session),
):
    return ok([
        {
            "id": t.id, "name": t.name, "prefix": t.prefix, "scopes": t.scopes,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "revoked": t.revoked_at is not None,
        }
        for t in tokens.list_tokens(session)
    ])


@router.post("/tokens")
def create_token(
    body: TokenIn,
    principal: Principal = Depends(require_scope("admin")),
    session: Session = Depends(get_session),
):
    raw, row = tokens.create_token(
        session, name=body.name, scopes=body.scopes,
        created_by=principal.name, expires_days=body.expires_days,
    )
    audit.record(
        session, actor=principal.name, actor_kind="token" if principal.kind == "token" else "user",
        action="create", object_type="token", object_id=row.id, label=row.name,
        changes=[{"field": "scope", "old": None, "new": row.scopes}],
        note="通过 API 签发令牌", request_id=uuid.uuid4().hex[:12],
    )
    session.commit()
    return ok({
        "id": row.id, "name": row.name, "scopes": row.scopes,
        "token": raw,
        "hint": "明文令牌只显示这一次，请立刻保存",
    }, status=201)


@router.delete("/tokens/{token_id}")
def revoke_token(
    token_id: int,
    _p: Principal = Depends(require_scope("admin")),
    session: Session = Depends(get_session),
):
    if not tokens.revoke(session, token_id):
        raise ApiError("NOT_FOUND", "令牌不存在或已撤销")
    audit.record(
        session, actor=_p.name, actor_kind="token" if _p.kind == "token" else "user",
        action="delete", object_type="token", object_id=token_id, label=f"令牌 #{token_id}",
        changes=[{"field": "状态", "old": "有效", "new": "已吊销"}],
        note="通过 API 吊销令牌", request_id=uuid.uuid4().hex[:12],
    )
    session.commit()
    return ok({"id": token_id, "revoked": True})


# ── 审计日志（只读）────────────────────────────────────────────────────────
@router.get("/audit")
def get_audit_log(
    since: str | None = Query(default=None, description="ISO 时间，含"),
    until: str | None = Query(default=None, description="ISO 时间，含"),
    actor: str | None = None,
    action: str | None = None,
    object_type: str | None = None,
    origin: str | None = None,
    q: str | None = Query(default=None, description="型号 / 指标 / 值 / 备注 关键词"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _p: Principal = Depends(require_scope("admin")),
    session: Session = Depends(get_session),
):
    """「谁改了什么」的机器可读版本。要求 admin scope —— 日志本身是敏感数据。"""

    def _ts(value: str | None, field: str) -> datetime | None:
        if not value:
            return None
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ApiError("VALIDATION_FAILED", f"{field} 不是合法 ISO 时间", field=field) from None
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    rows, total = audit.query(
        session,
        since=_ts(since, "since"),
        until=_ts(until, "until"),
        actor=actor, action=action, object_type=object_type, origin=origin, q=q,
        limit=limit, offset=offset,
    )
    return ok(
        [
            {
                "id": r.id,
                "at": r.at.isoformat() if r.at else None,
                "actor": r.actor,
                "actor_kind": r.actor_kind,
                "action": r.action,
                "object_type": r.object_type,
                "object_id": r.object_id,
                "object_label": r.object_label,
                "origin": r.origin,
                "request_id": r.request_id,
                "changes": audit.parse_changes(r),
                "note": r.note,
            }
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── 发现面：给 AI 的工具清单 ───────────────────────────────────────────────
@router.get("/tools")
def tool_manifest(_p: Principal = Depends(require_scope("read"))):
    """按 OpenAI function-calling 的形态描述关键操作。

    单独维护而不从 OpenAPI 自动生成：OpenAPI 的 schema 是给代码生成器看的，
    这里的 descriptions 是给模型看的，两者关注点不同，硬合并会两头都不好用。
    """
    tools = [
        {
            "name": "get_context",
            "method": "GET", "path": "/api/v1/context",
            "description": "一次拿到库里的骨架：公司、型号、指标 key 与数量。动手前先调它。",
            "scope": "read",
        },
        {
            "name": "search_products",
            "method": "GET", "path": "/api/v1/products",
            "description": "按公司/大类/状态/型号关键词筛选型号，分页用 cursor。",
            "scope": "read",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "integer"},
                    "category": {"type": "string", "enum": list(config.CATEGORY_LABEL)},
                    "status": {"type": "string", "enum": ["active", "eol", "rumored"]},
                    "q": {"type": "string", "description": "型号模糊搜索"},
                    "limit": {"type": "integer", "maximum": 200, "default": 50},
                    "cursor": {"type": "integer", "description": "上一页最后一条 id"},
                },
            },
        },
        {
            "name": "get_product",
            "method": "GET", "path": "/api/v1/products/{product_id}",
            "description": "型号详情：全部参数、缺失项、完整度、ETag。",
            "scope": "read",
        },
        {
            "name": "compare_products",
            "method": "GET", "path": "/api/v1/matrix",
            "description": "横向对比：返回指标行与各型号的值、是否缺失、是否最优。",
            "scope": "read",
            "parameters": {
                "type": "object",
                "properties": {"p": {"type": "string", "description": "型号 id，逗号分隔"}},
            },
        },
        {
            "name": "get_changes",
            "method": "GET", "path": "/api/v1/changes",
            "description": "增量同步：拉取 since 之后变更过的参数。",
            "scope": "read",
            "parameters": {
                "type": "object", "required": ["since"],
                "properties": {"since": {"type": "string", "format": "date-time"},
                               "limit": {"type": "integer", "maximum": 1000}},
            },
        },
        {
            "name": "create_product",
            "method": "POST", "path": "/api/v1/products",
            "description": "新建型号；公司可传 entity_id 或 entity_name。建议先 dry_run=true 预演。",
            "scope": "write",
            "parameters": {
                "type": "object", "required": ["model", "category"],
                "properties": {
                    "entity_id": {"type": "integer"},
                    "entity_name": {"type": "string"},
                    "model": {"type": "string"},
                    "category": {"type": "string", "enum": list(config.CATEGORY_LABEL)},
                    "name": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "eol", "rumored"]},
                    "force": {"type": "boolean", "description": "公司名相似时强制新建"},
                    "dry_run": {"type": "boolean"},
                },
            },
        },
        {
            "name": "update_product",
            "method": "PATCH", "path": "/api/v1/products/{product_id}",
            "description": "改型号元信息。传 If-Match 防覆盖；已有参数时不能改大类。",
            "scope": "write",
        },
        {
            "name": "write_specs",
            "method": "POST", "path": "/api/v1/specs/batch",
            "description": "批量写参数。metric_key 用 key 不用中文名；逐项返回成败。",
            "scope": "write",
            "parameters": {
                "type": "object", "required": ["items"],
                "properties": {
                    "dry_run": {"type": "boolean"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["product_id", "metric_key"],
                            "properties": {
                                "product_id": {"type": "integer"},
                                "metric_key": {"type": "string"},
                                "action": {"type": "string", "enum": ["upsert", "delete"]},
                                "value_num": {"type": "number"},
                                "value_text": {"type": "string"},
                                "condition": {"type": "string", "description": "如 @15Vpp"},
                                "verified": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        },
        {
            "name": "bulk_import",
            "method": "POST", "path": "/api/v1/import",
            "description": "按行导入参数（与 Excel 导入同规则），支持 dry_run。",
            "scope": "ingest",
        },
        {
            "name": "get_change_log",
            "method": "GET", "path": "/api/v1/audit",
            "description": "查变更日志：谁在什么时候把哪个型号的哪项参数从多少改成了多少。需 admin scope。",
            "scope": "admin",
            "parameters": {
                "type": "object",
                "properties": {
                    "since": {"type": "string", "format": "date-time"},
                    "until": {"type": "string", "format": "date-time"},
                    "actor": {"type": "string"},
                    "action": {"type": "string", "enum": ["create", "update", "delete", "import"]},
                    "object_type": {"type": "string", "enum": ["product", "spec", "entity", "user", "token", "setting"]},
                    "q": {"type": "string", "description": "型号 / 指标 / 值关键词"},
                    "limit": {"type": "integer", "maximum": 500},
                    "offset": {"type": "integer"},
                },
            },
        },
        {
            "name": "delete_product",
            "method": "DELETE", "path": "/api/v1/products/{product_id}?confirm=true",
            "description": "删除型号及其全部参数，不可撤销。",
            "scope": "write",
        },
    ]
    return ok({
        "tools": tools,
        "auth": "Authorization: Bearer <token>",
        "conventions": [
            "所有响应都是 {ok,data,meta}；失败看 error.code，不要解析 message。",
            "写操作先 dry_run=true 预演，确认后再落库。",
            "重试写操作时带同一个 Idempotency-Key，不会写两遍。",
            "metric_key 用稳定的 key，不要用中文指标名（名称可能改）。",
            "status 只会是 active / eol / rumored。",
        ],
    })
