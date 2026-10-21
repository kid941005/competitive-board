"""FastAPI 装配层：应用、生命周期、异常处理、路由注册。

页面是服务端渲染（Jinja2 + 原生 JS），刻意不引入前端构建链：
一条 `uvicorn app.main:app` 就能跑起来。

代码分布（曾经是 2100 行的单个文件，连 api_v1 都要局部导入它来取矩阵计算）：
    main.py            装配：app / lifespan / 异常处理 / 健康检查
    selection.py       选中与矩阵组装（页面、导出、/api/v1 共用）
    web_shared.py      模板环境、权限依赖、表单校验、公司/型号查找
    routes_auth.py     登录、登出、首页
    routes_compare.py  对比页、只读 JSON、导出
    routes_products.py 产品管理与批量导入
    routes_datasheet.py 规格书导入
    routes_admin.py    账号 / 令牌 / 日志 / 系统参数

权限边界见 auth.py 的 ROLE_PERMISSIONS —— 所有路由只声明「需要哪个权限位」，
不出现任何 role 字符串比较。接口层缺权限一律 403、未登录一律 401（HTML 则跳登录页）。
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from . import api_v1, audit, auth, config, datasheet_store, settings_store
from .db import engine, init_db
from .models import Entity
from .routes_admin import router as admin_router
from .routes_auth import router as auth_router
from .routes_compare import router as compare_router
from .routes_datasheet import router as datasheet_router
from .routes_products import router as products_router
from .timeutil import fmt_local
from .web_shared import BASE, templates

# 用户不存在时也走一次哈希校验，避免通过响应时间枚举账号


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    admin_name = (os.getenv("CIB_ADMIN_USER") or "admin").strip() or "admin"
    with Session(engine) as session:
        auth.purge_expired(session)
        generated = auth.ensure_bootstrap_admin(session)
        # 老数据卷里自家公司可能还叫「本公司」，启动时改成真实名称（幂等）
        _rename_legacy_self(session)
        # 日志会无限增长，按保留期在启动时清一次
        d = settings_store.all_values(session)
        audit.purge(session, int(d.get("audit.retention_days", audit.RETENTION_DEFAULT_DAYS)))
        # 规格书原文同理：没被任何参数引用、且超过保留期的，连同解析结果一起删
        swept = datasheet_store.purge_orphans(
            session, int(d.get("datasheet.keep_days", 30))
        )
        if swept["removed"]:
            print(
                f"[startup] 清理未引用规格书 {swept['removed']} 份"
                f"（{swept['bytes'] / 1024 / 1024:.1f} MB），"
                f"保留在用 {swept['referenced']} 份、未到期 {swept['kept']} 份",
                flush=True,
            )
    if generated:
        # 只在此打印一次。请立即登录后修改密码。
        print("=" * 62, flush=True)
        print(f"  已创建初始管理员：{admin_name}", flush=True)
        print(f"  初始密码：{generated}", flush=True)
        print("  登录后请立刻在「账号管理」里修改密码。", flush=True)
        print("=" * 62, flush=True)
    yield


app = FastAPI(title="竞品情报看板", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
# 机器调用接口面（AI / 脚本）。独立命名空间，旧路由一条未动。
app.include_router(api_v1.router)
# 页面路由按访问面拆分，顺序即匹配优先级（具体路径先于带参数的路径）
app.include_router(auth_router)
app.include_router(compare_router)
app.include_router(products_router)
app.include_router(datasheet_router)
app.include_router(admin_router)
# 静态资源版本号：前端改动后改 app.version 即可让浏览器重新拉取，
# 避免「代码已修复但用户还跑着缓存里的旧脚本」这种假象。
templates.env.globals["asset_v"] = app.version
# 页脚显示的版本号：与 app.version 同源（CI 会校验这个字面值与各 compose/README 一致），
# 用户报问题时一眼能说出「我这是哪一版」。
templates.env.globals["app_version"] = app.version
# 库里存 UTC、界面按部署时区显示：模板里一律用 {{ ts | localtime }}，不要再裸 strftime
templates.env.filters["localtime"] = fmt_local


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
@app.exception_handler(api_v1.ApiError)
async def _on_api_error(_request: Request, exc: api_v1.ApiError):
    return api_v1.fail(exc)


_V1_CODE_BY_STATUS = {
    400: "VALIDATION_FAILED",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "VALIDATION_FAILED",
    409: "CONFLICT_VERSION",
    422: "VALIDATION_FAILED",
}
@app.exception_handler(HTTPException)
async def _on_http_error(request: Request, exc: HTTPException):
    """页面错误渲染成页面，接口错误保持 JSON；/api/v1 走统一信封。

    原实现对页面路由也返回 {"detail": ...}：用户在表单里点保存，
    结果浏览器跳到一屏裸 JSON，既没有导航也没有返回按钮。
    """
    if not request.url.path.startswith("/api/v1"):
        if _wants_html(request):
            back = request.headers.get("referer") or "/compare"
            return templates.TemplateResponse(
                request=request,
                name="error.html",
                context={
                    "status": exc.status_code,
                    "message": exc.detail,
                    "back": back,
                },
                status_code=exc.status_code,
            )
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )
    return JSONResponse(
        {
            "ok": False,
            "error": {
                "code": _V1_CODE_BY_STATUS.get(exc.status_code, "VALIDATION_FAILED"),
                "message": str(exc.detail),
            },
            "meta": {"request_id": uuid.uuid4().hex[:12]},
        },
        status_code=exc.status_code,
        headers=exc.headers,
    )
@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError):
    errors = jsonable_encoder(exc.errors())
    if not request.url.path.startswith("/api/v1"):
        return JSONResponse({"detail": errors}, status_code=422)
    return JSONResponse(
        {
            "ok": False,
            "error": {
                "code": "VALIDATION_FAILED",
                "message": "请求参数校验未通过",
                "hint": "details 里逐项给出字段与原因",
                "details": errors,
            },
            "meta": {"request_id": uuid.uuid4().hex[:12]},
        },
        status_code=422,
    )
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
@app.exception_handler(IntegrityError)
async def _on_integrity(request: Request, _exc: IntegrityError):
    """唯一索引挡住并发写入时，回 409 而不是 500。

    有了 (型号, 指标) 与 (公司, 型号) 的唯一索引，两条并发请求里必有一条会失败。
    代码里的「先查再插」挡不住这个时间窗，数据库能挡；剩下要做的只是把
    IntegrityError 翻译成「冲突，请重试」，而不是让用户看一屏堆栈。
    """
    message = "写入冲突：同一型号或同一参数已存在，可能是并发提交。请刷新后重试。"
    if request.url.path.startswith("/api/v1"):
        return api_v1.fail(api_v1.ApiError("UNIQUE_CONFLICT", message))
    if _wants_html(request):
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"status": 409, "message": message,
                     "back": request.headers.get("referer") or "/compare"},
            status_code=409,
        )
    return JSONResponse({"detail": message, "code": "unique_conflict"}, status_code=409)
def _rename_legacy_self(session: Session) -> None:
    """把历史占位名（本公司 / 本方 / 我方）改成真实公司名。

    只改 kind=self 且名字恰好是占位名的那一条，改名本身不产生日志 ——
    它不是业务数据变更，而是本次文案统一的一部分。
    """
    for ent in session.exec(select(Entity).where(Entity.kind == "self")).all():
        if ent.name in config.SELF_LEGACY_NAMES:
            old = ent.name           # 先存旧名：原实现先赋值再打印，输出成了「左蓝微 → 左蓝微」
            ent.name = config.SELF_COMPANY_NAME
            session.add(ent)
            print(f"[startup] 自家公司名 {old} → {config.SELF_COMPANY_NAME}", flush=True)
    session.commit()
@app.get("/healthz")
def healthz():
    """容器健康检查用，不鉴权。带版本号：镜像升级后能一眼确认跑的是哪个版本。"""
    return {"ok": True, "version": app.version}
