"""数据库引擎与会话。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from . import config, schema
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
    """建表 → 升版本。两步缺一不可：

    - `create_all` 只补「缺的表」，不会改已有表，对新库足够；
    - 对老库（0.7.x 及更早），字段与索引的演进必须走 schema.migrate，
      否则升级镜像后数据卷仍是旧结构，查询时才报 no such column。
    """
    SQLModel.metadata.create_all(engine)
    result = schema.migrate(engine)
    if result["applied"]:
        print(f"[db] 表结构 {result['from']} → {result['to']}：" + "；".join(result["applied"]),
              flush=True)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
