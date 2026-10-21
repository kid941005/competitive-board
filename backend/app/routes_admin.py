"""管理员面：账号、API 令牌、变更日志、系统参数。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from . import audit, auth, settings_store, tokens
from . import seed as seed_module
from .auth import P_SETTINGS, P_USERS, AuthSession, User
from .db import get_session
from .models import SpecMetric
from .web_shared import _ctx, _perm, _perm_user, _redirect, _rid, templates

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MAX_DISPLAY_NAME = 32
LOG_ACTIONS = (("create", "新建"), ("update", "修改"), ("delete", "删除"), ("import", "导入"))
LOG_OBJECTS = (
    ("product", "型号"), ("spec", "参数"), ("entity", "公司"),
    # adoption = 客户采用关系；metric = 指标字典（迁移会写这条）
    ("adoption", "客户采用"), ("metric", "指标"),
    ("user", "账号"), ("token", "令牌"), ("setting", "系统参数"),
)

router = APIRouter()

def _active_admin_count(session: Session) -> int:
    rows = session.exec(select(User).where(User.role == "admin", User.is_active == True)).all()
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


@router.post("/admin/users/{uid}/profile")
def admin_update_profile(
    uid: int,
    username: str = Form(...),
    display_name: str = Form(default=""),
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_USERS)),
    session: Session = Depends(get_session),
):
    """改账号名与显示名。

    账号名就是登录名：改完旧名立即无法登录、新名生效。**当前会话不受影响** ——
    会话表按 user_id 关联，不认账号名，所以改自己的名字不会被踢出去。
    历史审计里的 actor 是当时写下的字符串，改名后仍显示旧名，这是对的（历史就是历史）。
    """
    # actor 与 target 可能是同一行（管理员改自己）。先取快照：若先改名，
    # 「谁做的」会被写成新名字，日志与历史对不上。
    actor_name = actor.username
    target = session.get(User, uid)
    if target is None:
        return _redirect("/admin/users", "账号不存在", "err")

    name = username.strip()
    if not USERNAME_RE.match(name):
        return _redirect("/admin/users", "账号名需为 3–32 位字母、数字、下划线、点或短横线", "err")
    display = display_name.strip()
    if len(display) > MAX_DISPLAY_NAME:
        return _redirect("/admin/users", f"显示名最多 {MAX_DISPLAY_NAME} 个字符", "err")

    # 唯一性自己先拦一道：唯一索引也会拦，但那会抛 IntegrityError 变成 500
    clash = session.exec(select(User).where(User.username == name)).first()
    if clash is not None and clash.id != target.id:
        return _redirect("/admin/users", f"账号名 {name} 已被占用", "err")

    old_name, old_display = target.username, (target.display_name or "")
    changes: list[dict] = []
    if name != old_name:
        changes.append({"field": "账号", "old": old_name, "new": name})
        target.username = name
    if display != old_display:
        changes.append({"field": "显示名", "old": old_display or None, "new": display or None})
        target.display_name = display or None

    if not changes:
        return _redirect("/admin/users", "账号信息没有变化")

    if len(changes) == 2:
        note = "同时改了账号名与显示名；旧账号名立即无法登录"
    elif changes[0]["field"] == "账号":
        note = "改账号名；旧账号名立即无法登录，当前会话不受影响"
    else:
        note = "改显示名"

    session.add(target)
    audit.record(
        session, actor=actor_name, action="update", object_type="user",
        object_id=target.id, label=old_name, changes=changes,
        note=note, request_id=_rid(),
    )
    session.commit()

    if name != old_name:
        return _redirect("/admin/users", f"{old_name} 已改名为 {name}（下次登录请用新账号名）")
    return _redirect("/admin/users", f"{target.username} 的显示名已改为 {display or '（空）'}")


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
# LOG_ACTIONS / LOG_OBJECTS 定义在文件顶部（这里曾经又定义了一遍，两处内容一模一样，
# 改一处漏一处的风险大于「就近可读」的好处）。


@router.get("/admin/logs", response_class=HTMLResponse)
def admin_logs_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_USERS)),
    session: Session = Depends(get_session),
    days: int = Query(default=7, ge=1, le=3650),
    # 清理日志的 POST 会带 ?ok=已清理 N 条 重定向回来，模板里也有 {% if ok %}，
    # 但这里原先没接这两个参数 —— 于是那句提示永远不显示。
    ok: str | None = Query(default=None),
    err: str | None = Query(default=None),
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
            ok=ok,
            err=err,
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


@router.post("/admin/settings/apply-metrics-template")
def admin_apply_metrics_template(
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_SETTINGS)),
    session: Session = Depends(get_session),
):
    """把内置的压电指标字典写进空库。

    空库启动后这是「从零到能用」的关键一步：对比页的行来自指标字典，
    字典为空时导入进去了也匹配不到、页面上什么都不显示。
    只写指标与适用大类，不建任何公司/型号 —— 那些应当由用户自己导入。
    """
    before = len(session.exec(select(SpecMetric)).all())
    try:
        result = seed_module.seed(metrics_only=True)
    except Exception as exc:
        return _redirect("/admin/settings", f"写入失败：{exc}", "err")

    if result.get("skipped"):
        return _redirect(
            "/admin/settings",
            f"库里已有 {before} 个指标，未做改动（要重来请先清空）",
            "err",
        )

    audit.record(
        session, actor=actor.username, action="create", object_type="setting",
        label="指标字典", origin="web", request_id=_rid(),
        changes=[{"field": "指标数", "old": str(before), "new": str(result.get("metrics", 0))}],
        note="应用内置压电指标模板（只写字典，不含公司/型号）",
    )
    session.commit()
    return _redirect(
        "/admin/settings",
        f"已写入 {result.get('metrics', 0)} 个指标，未创建任何公司或型号",
    )


# ── 健康检查（容器用，不过鉴权）───────────────────────────────────────────
