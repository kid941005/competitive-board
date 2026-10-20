"""登录、登出与首页跳转。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from . import auth, config, settings_store, throttle
from .auth import COOKIE_NAME, P_VIEW, User
from .db import get_session
from .web_shared import _perm, _safe_next, templates

router = APIRouter()

# 用户不存在时也走一次哈希校验，避免通过响应时间枚举账号
_DUMMY_HASH = auth.hash_password("__nonexistent__")

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = Query(default="/compare")):
    return templates.TemplateResponse(
        request=request, name="login.html", context={"next": _safe_next(next), "error": None}
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/compare"),
    session: Session = Depends(get_session),
):
    name = username.strip()
    # 先看是否在退避窗口里：命中就直接回 429，连密码哈希都不算（省 CPU 也是防刷的一环）
    client = request.client.host if request.client else "-"
    wait = throttle.check(client, name)
    if wait:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "next": _safe_next(next),
                "error": f"尝试过于频繁，请 {wait} 秒后再试",
            },
            status_code=429,
            headers={"Retry-After": str(wait)},
        )

    user = session.exec(select(User).where(User.username == name)).first()

    # 无论账号是否存在都做一次校验，抹平时间差
    ok = auth.verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not ok or not user.is_active:
        throttle.fail(client, name)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"next": _safe_next(next), "error": "账号或密码错误，或账号已停用"},
            status_code=401,
        )

    throttle.succeed(client, name)

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


@router.post("/logout")
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


@router.get("/", include_in_schema=False)
def index(request: Request, pair=Depends(_perm(P_VIEW))) -> RedirectResponse:
    return RedirectResponse(url="/compare")
