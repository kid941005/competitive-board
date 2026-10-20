"""API 令牌：签发、校验、吊销。

设计要点：
- 令牌形如 `cib_<prefix>_<secret>`。库里只存 sha256(secret)，泄库拿不到可用令牌。
- prefix 用于索引定位，避免为了校验而全表扫描。
- scope 是独立词汇（read/write/ingest/admin），不复用角色的权限位 ——
  角色是给人看的，scope 是给程序看的；两者映射关系写在一处，改角色不影响已签发的令牌。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from .models import ApiToken

TOKEN_PREFIX = "cib"
VALID_SCOPES = ("read", "write", "ingest", "admin")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def scopes_of(token: ApiToken) -> frozenset[str]:
    return frozenset(s.strip() for s in (token.scopes or "").split(",") if s.strip())


def normalize_scopes(raw: str | list[str] | None) -> str:
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(",")]
    else:
        items = [str(s).strip() for s in (raw or [])]
    picked = [s for s in dict.fromkeys(items) if s in VALID_SCOPES]
    return ",".join(picked) or "read"


def create_token(
    session: Session,
    *,
    name: str,
    scopes: str | list[str] | None = "read",
    created_by: str | None = None,
    expires_days: int | None = None,
) -> tuple[str, ApiToken]:
    """返回 (明文令牌, 记录)。明文只在此刻可见，之后无法再取回。"""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(24)
    row = ApiToken(
        name=(name or "").strip() or "未命名令牌",
        prefix=f"{TOKEN_PREFIX}_{prefix}",
        token_hash=_hash(secret),
        scopes=normalize_scopes(scopes),
        created_by=created_by,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=expires_days) if expires_days else None
        ),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return f"{TOKEN_PREFIX}_{prefix}_{secret}", row


def verify(session: Session, raw: str) -> ApiToken | None:
    """校验明文令牌。失败一律返回 None，不区分「不存在 / 已撤销 / 过期」，
    避免把令牌是否存在泄露给调用方。"""
    raw = (raw or "").strip()
    # 只拆前两段：secret 是 urlsafe base64，本身可能含下划线（"-" "_" 都在字母表里）。
    # 早期用 split("_") 全拆并要求恰好 3 段，导致约 1/3 的令牌刚签发就被判无效。
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not parts[2]:
        return None
    prefix, secret = f"{parts[0]}_{parts[1]}", parts[2]
    row = session.exec(select(ApiToken).where(ApiToken.prefix == prefix)).first()
    if row is None or not row.token_hash:
        return None
    if row.revoked_at is not None:
        return None
    if row.expires_at is not None:
        expires = row.expires_at
        if expires.tzinfo is None:  # SQLite 读回来可能没有时区
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            return None
    if not hmac.compare_digest(row.token_hash, _hash(secret)):
        return None
    return row


def touch(session: Session, row: ApiToken, min_interval_seconds: int = 60) -> None:
    """记录最近使用时间。

    AI 会高频轮询读接口，每个请求都 UPDATE + COMMIT 在 SQLite 下就是「读被写放大」
    （每次 commit 一次 fsync）。一分钟内只落库一次，精度对「最近使用」这个用途足够。
    """
    now = datetime.now(timezone.utc)
    last = row.last_used_at
    if last is not None:
        if last.tzinfo is None:      # SQLite 读回来可能没有时区
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < min_interval_seconds:
            return
    row.last_used_at = now
    session.add(row)
    session.commit()


def revoke(session: Session, token_id: int) -> bool:
    row = session.get(ApiToken, token_id)
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    return True


def list_tokens(session: Session) -> list[ApiToken]:
    return list(session.exec(select(ApiToken).order_by(ApiToken.id.desc())).all())
