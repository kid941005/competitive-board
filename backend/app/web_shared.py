"""页面路由共用的小工具：模板环境、权限依赖、表单校验、跳转。

这几样被 5 个路由模块同时用到。如果留在 main.py，路由要 import main，
而 main 又要 import 路由来注册 —— 典型的循环依赖。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, config
from .auth import AuthSession, User
from .models import PRODUCT_STATUSES
from .timeutil import fmt_local

BASE = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE / "templates"))
# 库里存 UTC、界面按部署时区显示：模板里一律用 {{ ts | localtime }}，不要裸 strftime
templates.env.filters["localtime"] = fmt_local


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


def _rid() -> str:
    """一次请求的关联 id：批量操作会产生多行日志，靠它折叠成一组。"""
    return uuid.uuid4().hex[:12]


def _ctx(user: User, auth_session: AuthSession, sset: dict, **extra) -> dict:
    ctx = auth.view_ctx(user, auth_session)
    ctx["sset"] = sset
    ctx.update(extra)
    return ctx


def _safe_next(target: str | None) -> str:
    """只允许站内相对路径，防开放重定向。"""
    if (
        not target
        or not target.startswith("/")
        or target.startswith("//")
        or chr(92) in target        # 反斜杠：部分浏览器把 /\host 当 //host
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in target)
    ):
        return "/compare"
    return target


# ── 表单校验 ────────────────────────────────────────────────────────────────


def _validate_status(status: str | None) -> str:
    value = (status or "active").strip()
    if value not in PRODUCT_STATUSES:
        raise HTTPException(400, f"状态只能是 {' / '.join(PRODUCT_STATUSES)}")
    return value


def _validate_target(category: str, model: str) -> str:
    if category not in config.CATEGORY_LABEL:
        raise HTTPException(400, f"未知产品大类「{category}」，可选：{'、'.join(config.CATEGORY_LABEL.values())}")
    model = (model or "").strip()
    if not model:
        raise HTTPException(400, "请填写规格书对应的型号")
    if len(model) > 64:
        raise HTTPException(400, "型号过长（≤ 64 字符）")
    return model


def _redirect(url: str, msg: str | None = None, kind: str = "ok") -> RedirectResponse:
    if msg:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{kind}={quote(msg)}"
    return RedirectResponse(url, status_code=303)
