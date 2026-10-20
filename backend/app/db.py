"""数据库引擎与会话。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from . import config
from .models import (  # noqa: F401  确保建表时模型已注册
    ApiIdempotency,
    ApiToken,
    AuditLog,
    Entity,
    Product,
    ProductSpec,
    SpecMetric,
    SpecSource,
)

_url = config.database_url()
_is_sqlite = _url.startswith("sqlite")

engine = create_engine(
    _url,
    echo=False,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=not _is_sqlite,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - 连接级设置
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
