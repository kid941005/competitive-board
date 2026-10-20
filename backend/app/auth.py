"""账号、会话与权限。

权限模型分两层：**角色 → 权限位 → 接口检查权限位**。
路由只调用 `require(P_XXX)`，从不判断 role 字符串。这样以后加第三种角色
（比如只读分析师、数据维护员）时，只需要改 ROLE_PERMISSIONS 一行，不用动任何路由。

安全基线：
- 密码 pbkdf2_sha256 + 每用户 16 字节随机盐 + 24 万次迭代
- 会话表只存 token 的 sha256，不存明文；库泄露也无法直接冒用
- Cookie HttpOnly + SameSite=Lax；HTTPS 部署时设 CIB_COOKIE_SECURE=1
- 所有非幂等请求校验 CSRF 双重提交令牌（表单字段 _csrf 或请求头 X-CSRF-Token）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request
from sqlmodel import Session, select

from .db import get_session
from .models import AuthSession, User, utcnow

COOKIE_NAME = "cib_session"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "_csrf"

# ── 权限位 ─────────────────────────────────────────────────────────────────
P_VIEW = "data:view"          # 查看对比矩阵、溯源
P_EXPORT = "data:export"      # 导出 CSV / JSON
P_WRITE = "data:write"        # 导入规格书、CSV 导入、写入参数
P_USERS = "admin:users"       # 账号管理
P_SETTINGS = "admin:settings"  # 系统参数

ALL_PERMISSIONS = (P_VIEW, P_EXPORT, P_WRITE, P_USERS, P_SETTINGS)

# 角色 → 权限。**这是唯一的权限真相来源。**
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset(ALL_PERMISSIONS),
    # 普通用户：只读。能看、能筛、能导出，但改不了任何数据，更进不了管理面。
    # 若希望业务人员也能导入规格书，在此加入 P_WRITE 即可，无需改路由。
    "viewer": frozenset({P_VIEW, P_EXPORT}),
}

ROLE_LABEL = {"admin": "管理员", "viewer": "普通用户"}

# ── 密码 ───────────────────────────────────────────────────────────────────
_ALGO = "pbkdf2_sha256"
_ITERATIONS = 240_000
MIN_PASSWORD_LEN = 10


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        salt = _b64d(salt_b64)
        expected = _b64d(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def check_password_policy(password: str) -> str | None:
    """返回错误说明；合规返回 None。"""
    if len(password) < MIN_PASSWORD_LEN:
        return f"密码至少 {MIN_PASSWORD_LEN} 位"
    if password.strip() != password:
        return "密码首尾不能有空白字符"
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "密码需同时包含字母与数字"
    return None


# ── 会话 ───────────────────────────────────────────────────────────────────
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    session: Session, user: User, hours: int, user_agent: str | None = None
) -> tuple[str, str]:
    """返回 (cookie 值, csrf 令牌)。cookie 值只在此处出现一次。"""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    session.add(
        AuthSession(
            token_hash=_token_hash(token),
            user_id=user.id,  # type: ignore[arg-type]
            csrf=csrf,
            expires_at=utcnow() + timedelta(hours=hours),
            user_agent=(user_agent or "")[:200] or None,
        )
    )
    user.last_login_at = utcnow()
    session.add(user)
    session.commit()
    return token, csrf


def resolve_session(session: Session, token: str) -> tuple[User, AuthSession] | None:
    """校验会话有效性：存在、未过期、账号仍启用。"""
    if not token:
        return None
    row = session.exec(
        select(AuthSession).where(AuthSession.token_hash == _token_hash(token))
    ).first()
    if row is None:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:  # SQLite 读回来会丢时区
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        session.delete(row)
        session.commit()
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    return user, row


def revoke_session(session: Session, token: str) -> None:
    if not token:
        return
    row = session.exec(
        select(AuthSession).where(AuthSession.token_hash == _token_hash(token))
    ).first()
    if row:
        session.delete(row)
        session.commit()


def revoke_user_sessions(session: Session, user_id: int) -> int:
    """改密或停用账号时，踢掉该用户全部会话。"""
    rows = session.exec(select(AuthSession).where(AuthSession.user_id == user_id)).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)


def purge_expired(session: Session) -> int:
    now = datetime.now(timezone.utc)
    rows = session.exec(select(AuthSession)).all()
    dropped = 0
    for row in rows:
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < now:
            session.delete(row)
            dropped += 1
    if dropped:
        session.commit()
    return dropped


def permissions_for(user: User) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(user.role, frozenset())


def has_perm(user: User, perm: str) -> bool:
    return perm in permissions_for(user)


# ── 异常：接口层与页面层分别处置 ───────────────────────────────────────────
class NotAuthenticated(Exception):
    """未登录。HTML 请求重定向到登录页，API 请求返回 401。"""


class Forbidden(Exception):
    """已登录但权限不足。HTML 返回 403 页面，API 返回 403 JSON。"""

    def __init__(self, perm: str, role: str):
        super().__init__(perm)
        self.perm = perm
        self.role = role


class CsrfInvalid(Exception):
    """CSRF 令牌缺失或不匹配。"""


# ── 依赖 ───────────────────────────────────────────────────────────────────
def get_current(
    request: Request, session: Session = Depends(get_session)
) -> tuple[User, AuthSession]:
    token = request.cookies.get(COOKIE_NAME, "")
    hit = resolve_session(session, token)
    if hit is None:
        raise NotAuthenticated()
    return hit


def require(perm: str):
    """生成一个「要求指定权限位」的依赖。路由写法：Depends(require(P_WRITE))。"""

    def dependency(
        pair: tuple[User, AuthSession] = Depends(get_current),
    ) -> User:
        user, _ = pair
        if not has_perm(user, perm):
            raise Forbidden(perm, user.role)
        return user

    return dependency


async def verify_csrf(request: Request, session: Session = Depends(get_session)) -> None:
    """非幂等请求的 CSRF 校验（双重提交）。

    只对 urlencoded 表单回退到读取表单字段 —— 不碰 multipart，
    否则会把上传的 body 提前消费掉，导致文件字段拿不到。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    token = request.cookies.get(COOKIE_NAME, "")
    hit = resolve_session(session, token)
    if hit is None:
        raise NotAuthenticated()
    _, auth_session = hit

    supplied = request.headers.get(CSRF_HEADER)
    if not supplied:
        ctype = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in ctype:
            form = await request.form()
            supplied = form.get(CSRF_FIELD)

    if not supplied or not hmac.compare_digest(str(supplied), auth_session.csrf):
        raise CsrfInvalid()


# ── 模板上下文 ─────────────────────────────────────────────────────────────
def view_ctx(user: User, auth_session: AuthSession) -> dict:
    perms = permissions_for(user)
    return {
        "user": user,
        "auth_session": auth_session,
        "is_admin": user.is_admin,
        "role_label": ROLE_LABEL.get(user.role, user.role),
        "can_write": P_WRITE in perms,
        "can_export": P_EXPORT in perms,
        "csrf": auth_session.csrf,
    }


# ── 初始化 ─────────────────────────────────────────────────────────────────
def ensure_bootstrap_admin(session: Session) -> str | None:
    """没有管理员时创建一个。返回自动生成的明文密码（仅此一次可见），否则 None。

    密码来源优先级：CIB_ADMIN_PASSWORD → 随机生成并打印到日志。
    显式配置的密码若不合规，直接抛错拒绝启动 —— 不静默降级成弱口令。
    """
    if session.exec(select(User).where(User.role == "admin")).first() is not None:
        return None

    username = (os.getenv("CIB_ADMIN_USER") or "admin").strip() or "admin"
    configured = (os.getenv("CIB_ADMIN_PASSWORD") or "").strip()

    if configured:
        problem = check_password_policy(configured)
        if problem:
            raise RuntimeError(f"CIB_ADMIN_PASSWORD 不合规：{problem}")
        password, generated = configured, False
    else:
        password, generated = secrets.token_urlsafe(12), True

    session.add(
        User(
            username=username,
            display_name="系统管理员",
            password_hash=hash_password(password),
            role="admin",
            created_by="bootstrap",
        )
    )
    session.commit()
    return password if generated else None
