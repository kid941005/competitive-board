"""管理员面：账号、API 令牌、变更日志、系统参数。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from . import audit, auth, settings_store, tokens
from .auth import P_SETTINGS, P_USERS, AuthSession, User
from .db import get_session
from .web_shared import _ctx, _perm, _perm_user, _redirect, _rid, templates

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
LOG_ACTIONS = (("create", "新建"), ("update", "修改"), ("delete", "删除"), ("import", "导入"))
LOG_OBJECTS = (
    ("product", "型号"), ("spec", "参数"), ("entity", "公司"),
    ("user", "账号"), ("token", "令牌"), ("setting", "系统参数"),
)

router = APIRouter()

def _active_admin_count(session: Session) -> int:
    rows = session.exec(select(User).where(User.role == "admin", User.is_active == True)).all()  # noqa: E712
    return len(rows)


@router.get("/admin/users", response_class=HTMLResponse)
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


@router.post("/admin/users/create")
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

    target = User(
        username=name,
        display_name=display_name.strip() or None,
        password_hash=auth.hash_password(password),
        role=role,
        created_by=actor.username,
    )
    session.add(target)
    session.flush()
    audit.record(
        session, actor=actor.username, action="create", object_type="user",
        object_id=target.id, label=name,
        changes=[{"field": "角色", "old": None, "new": auth.ROLE_LABEL[role]}],
        note="新建账号", request_id=_rid(),
    )
    session.commit()
    return _redirect("/admin/users", f"已创建 {name}（{auth.ROLE_LABEL[role]}）")


@router.post("/admin/users/{uid}/role")
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

    old_role = target.role
    target.role = role
    session.add(target)
    audit.record(
        session, actor=actor.username, action="update", object_type="user",
        object_id=target.id, label=target.username,
        changes=[{
            "field": "角色",
            "old": auth.ROLE_LABEL.get(old_role, old_role),
            "new": auth.ROLE_LABEL[role],
        }],
        note="调整角色，非管理员会话已失效", request_id=_rid(),
    )
    session.commit()
    if role != "admin":
        auth.revoke_user_sessions(session, target.id)  # type: ignore[arg-type]
    return _redirect("/admin/users", f"{target.username} 的角色已改为 {auth.ROLE_LABEL[role]}")


@router.post("/admin/users/{uid}/toggle")
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

    was_active = target.is_active
    target.is_active = not target.is_active
    session.add(target)
    audit.record(
        session, actor=actor.username, action="update", object_type="user",
        object_id=target.id, label=target.username,
        changes=[{
            "field": "状态",
            "old": "启用" if was_active else "停用",
            "new": "停用" if was_active else "启用",
        }],
        note="停用后其会话立即失效" if was_active else "重新启用账号", request_id=_rid(),
    )
    session.commit()
    if not target.is_active:
        auth.revoke_user_sessions(session, target.id)  # type: ignore[arg-type]
    state = "启用" if target.is_active else "停用"
    return _redirect("/admin/users", f"{target.username} 已{state}")


@router.post("/admin/users/{uid}/password")
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
    audit.record(
        session, actor=actor.username, action="update", object_type="user",
        object_id=target.id, label=target.username,
        changes=[{"field": "密码", "old": "（已设置）", "new": "（已重置）"}],
        note="重置密码，该账号全部会话已失效", request_id=_rid(),
    )
    session.commit()
    auth.revoke_user_sessions(session, target.id)  # type: ignore[arg-type]
    return _redirect("/admin/users", f"{target.username} 的密码已重置，其会话已全部失效")


# ── 管理员：API 令牌（给 AI / 脚本）──────────────────────────────────────


def _render_tokens(request, user, auth_session, session, *, ok=None, err=None,
                   new_token: str | None = None, new_token_name: str | None = None):
    sset = settings_store.all_values(session)
    return templates.TemplateResponse(
        request=request,
        name="admin_tokens.html",
        context=_ctx(
            user, auth_session, sset,
            page_title="API 令牌",
            page_sub="给 AI / 脚本用的机器凭证；每个令牌只显示一次",
            active="tokens",
            ok=ok, err=err,
            tokens=tokens.list_tokens(session),
            new_token=new_token,
            new_token_name=new_token_name,
            scope_options=[
                ("read", "读取", "查公司、型号、参数、对比矩阵、变更流"),
                ("write", "写入", "新建/修改型号、批量写参数、删除型号"),
                ("ingest", "批量摄取", "按行导入（与 Excel 导入同规则）"),
                ("admin", "管理", "签发/吊销令牌（隐含全部权限）"),
            ],
            api_base="/api/v1",
        ),
    )


@router.get("/admin/tokens", response_class=HTMLResponse)
def admin_tokens_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_USERS)),
    session: Session = Depends(get_session),
    ok: str | None = Query(default=None),
    err: str | None = Query(default=None),
):
    user, auth_session = pair
    return _render_tokens(request, user, auth_session, session, ok=ok, err=err)


@router.post("/admin/tokens/create", response_class=HTMLResponse)
async def admin_tokens_create(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_USERS)),
    session: Session = Depends(get_session),
    _csrf: None = Depends(auth.verify_csrf),
):
    user, auth_session = pair
    form = await request.form()
    name = str(form.get("name", "")).strip()
    scopes = [str(s) for s in form.getlist("scopes")]
    try:
        days = int(str(form.get("expires_days", "0")) or 0)
    except ValueError:
        days = 0
    if not name:
        return _render_tokens(request, user, auth_session, session, err="请填写令牌用途名称")
    if not scopes:
        return _render_tokens(request, user, auth_session, session, err="至少选择一个 scope")

    # 明文只在这里出现一次：不写日志、不进 URL（避免被访问日志收集）
    raw, row = tokens.create_token(
        session, name=name, scopes=scopes,
        created_by=user.username, expires_days=days or None,
    )
    audit.record(
        session, actor=user.username, action="create", object_type="token",
        object_id=row.id, label=name,
        changes=[{"field": "scope", "old": None, "new": row.scopes},
                 {"field": "有效期（天）", "old": None, "new": str(days or "长期")}],
        note="签发 API 令牌", request_id=_rid(),
    )
    session.commit()
    return _render_tokens(
        request, user, auth_session, session,
        new_token=raw, new_token_name=name,
        ok=f"已签发「{name}」，请立刻复制保存",
    )


@router.post("/admin/tokens/{token_id}/revoke")
def admin_tokens_revoke(
    token_id: int,
    _csrf: None = Depends(auth.verify_csrf),
    _pair: tuple[User, AuthSession] = Depends(_perm(P_USERS)),
    session: Session = Depends(get_session),
):
    operator, _ = _pair
    if not tokens.revoke(session, token_id):
        return _redirect("/admin/tokens", "令牌不存在或已撤销", "err")
    audit.record(
        session, actor=operator.username, action="delete", object_type="token",
        object_id=token_id, label=f"令牌 #{token_id}",
        changes=[{"field": "状态", "old": "有效", "new": "已吊销"}],
        note="吊销 API 令牌", request_id=_rid(),
    )
    session.commit()
    return _redirect("/admin/tokens", "令牌已吊销，立即失效")


# ── 管理员：变更日志 ───────────────────────────────────────────────────────
LOG_ACTIONS = (("create", "新建"), ("update", "修改"), ("delete", "删除"), ("import", "导入"))
LOG_OBJECTS = (
    ("product", "型号"), ("spec", "参数"), ("entity", "公司"),
    ("user", "账号"), ("token", "令牌"), ("setting", "系统参数"),
)


@router.get("/admin/logs", response_class=HTMLResponse)
def admin_logs_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_USERS)),
    session: Session = Depends(get_session),
    days: int = Query(default=7, ge=1, le=3650),
    actor: str | None = Query(default=None),
    action: str | None = Query(default=None),
    object_type: str | None = Query(default=None),
    origin: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    limit = 50
    rows, total = audit.query(
        session, since=since, actor=actor or None, action=action or None,
        object_type=object_type or None, origin=origin or None, q=q or None,
        limit=limit, offset=(page - 1) * limit,
    )
    items = [{"row": r, "changes": audit.parse_changes(r)} for r in rows]
    pages = max(1, (total + limit - 1) // limit)

    return templates.TemplateResponse(
        request=request,
        name="admin_logs.html",
        context=_ctx(
            user, auth_session, sset,
            page_title="变更日志",
            page_sub="谁、什么时候、从哪个入口，把什么改成了什么",
            active="logs",
            items=items,
            total=total,
            page=page,
            pages=pages,
            days=days,
            filters={"actor": actor or "", "action": action or "",
                     "object_type": object_type or "", "origin": origin or "", "q": q or ""},
            actors=audit.distinct_actors(session),
            action_options=LOG_ACTIONS,
            object_options=LOG_OBJECTS,
            origin_options=(("web", "页面"), ("datasheet", "规格书"), ("import", "表格导入"), ("api", "API")),
            retention=sset.get("audit.retention_days"),
        ),
    )


@router.post("/admin/logs/purge")
def admin_logs_purge(
    _csrf: None = Depends(auth.verify_csrf),
    pair: tuple[User, AuthSession] = Depends(_perm(P_USERS)),
    session: Session = Depends(get_session),
):
    user, _ = pair
    days = int(settings_store.all_values(session).get("audit.retention_days", audit.RETENTION_DEFAULT_DAYS))
    removed = audit.purge(session, days)
    if removed:
        audit.record(
            session, actor=user.username, action="delete", object_type="setting",
            label="变更日志", changes=[{"field": "清理条数", "old": str(removed), "new": "0"}],
            note=f"按保留期 {days} 天清理", request_id=_rid(),
        )
        session.commit()
    return _redirect("/admin/logs", f"已清理 {removed} 条超过 {days} 天的日志")


# ── 管理员：系统参数 ───────────────────────────────────────────────────────


@router.get("/admin/settings", response_class=HTMLResponse)
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


@router.post("/admin/settings")
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

    before = settings_store.all_values(session)
    changed, errors = settings_store.update_many(session, values, actor.username)
    if errors:
        return _redirect("/admin/settings", "；".join(errors), "err")

    after = settings_store.all_values(session)
    diff = [
        {"field": k, "old": audit._s(before.get(k)), "new": audit._s(after.get(k))}
        for k in values
        if audit._s(before.get(k)) != audit._s(after.get(k))
    ]
    if diff:
        audit.record(
            session, actor=actor.username, action="update", object_type="setting",
            label="系统参数", changes=diff, origin="web", request_id=_rid(),
        )
        session.commit()
    return _redirect("/admin/settings", f"已保存 {changed} 项配置")


# ── 健康检查（容器用，不过鉴权）───────────────────────────────────────────
