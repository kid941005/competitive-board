"""页面路由共用的小工具：模板环境、权限依赖、表单校验、公司/型号查找。

这几样被 5 个路由模块同时用到。如果留在 main.py，路由要 import main，
而 main 又要 import 路由来注册 —— 典型的循环依赖。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from . import auth, config
from .auth import AuthSession, User
from .models import Entity, Product
from .timeutil import fmt_local

BASE = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE / "templates"))
# 库里存 UTC、界面按部署时区显示：模板里一律用 {{ ts | localtime }}，不要裸 strftime
templates.env.filters["localtime"] = fmt_local

PRODUCT_STATUSES: tuple[str, ...] = ("active", "eol", "rumored")

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
    ):
        return "/compare"
    return target


# ── 数据装载 ────────────────────────────────────────────────────────────────


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


PRODUCT_STATUSES: tuple[str, ...] = ("active", "eol", "rumored")


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
