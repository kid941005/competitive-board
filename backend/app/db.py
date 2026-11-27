"""数据库引擎与会话。"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from . import config, schema
from .models import (  # noqa: F401  确保建表时模型已注册
    ApiIdempotency,
    ApiToken,
    AuditLog,
    Entity,
    NewsItem,
    NewsSource,
    Product,
    ProductSpec,
    SpecMetric,
    SpecSource,
)

log = logging.getLogger("cib.db")

_url = config.database_url()
_is_sqlite = _url.startswith("sqlite")
IS_SQLITE = _is_sqlite

engine = create_engine(
    _url,
    echo=False,
    # timeout 是 sqlite3 的**锁等待秒数**（默认 5 秒）：后台定时抓取在写库时，
    # 用户点登录/保存这类写请求会撞上写锁。5 秒不够就抛 "database is locked"，
    # 页面直接 500。放到 30 秒，把「偶发撞锁」变成「稍等一下」。
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=not _is_sqlite,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - 连接级设置
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def _diagnose_data_dir(path: Path) -> None:
    """提前确认数据目录可用，把 SQLite 那句含糊的报错换成能照着做的说明。

    `sqlite3.OperationalError: unable to open database file` 只有三种成因：
    目录不存在、目录存在但进程无写权限、磁盘满。三者报错文本完全相同，
    而容器部署里几乎都是第二种（uid 10001 对宿主目录没有写权限），
    原生信息一个字的线索都不给。这里逐项判定，失败时把 uid、属主、权限位
    和修复命令一起抛出去。
    """
    uid = getattr(os, "getuid", lambda: -1)()
    gid = getattr(os, "getgid", lambda: -1)()

    # 先判类型再建目录：路径上若已有一个同名**文件**，mkdir 会抛
    # FileExistsError（Windows 是 WinError 183「当文件已存在时无法创建该文件」），
    # 那句话看不出真正的原因是「CIB_DATA_DIR 指到一个文件上了」。
    if path.exists() and not path.is_dir():
        raise RuntimeError(
            f"数据目录 {path} 已存在但不是目录（像是个文件）。\n"
            f"  请检查 CIB_DATA_DIR 是否指错了位置。"
        )

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"数据目录 {path} 不存在且无法创建：{exc}\n"
            f"  容器内当前 uid={uid} gid={gid}。\n"
            f"  若用 bind mount 挂宿主目录，请确认父目录对该用户可写；"
            f"或改回 docker-compose.yml 默认的命名卷 appdata。"
        ) from exc

    # 只看 os.access 不够：它反映的是位掩码，容器里 ACL、只读挂载、
    # 上级目录 sticky 位都会让「看起来可写」实际写不进去。真写一个文件才作数。
    probe = path / f".write-probe-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        try:
            st = path.stat()
            owner = (f"目录属主 uid={st.st_uid} gid={st.st_gid} "
                     f"权限={oct(st.st_mode & 0o777)}")
        except OSError:
            owner = "目录属主不可读"
        # Windows 上 os.getuid 不存在，取到 -1 时不能把它塞进 chown，
        # 否则会打印出 `chown -R -1:-1` 这种照抄必错的命令。镜像内固定 10001。
        if uid >= 0:
            fix = f"    sudo chown -R {uid}:{gid} <数据目录>"
        else:
            fix = "    sudo chown -R 10001:10001 <数据目录>   # 镜像内固定 uid 10001"
        raise RuntimeError(
            f"数据目录 {path} 不可写（容器内 uid={uid} gid={gid}，{owner}）：{exc}\n"
            f"  这是容器部署最常见的启动失败原因：宿主目录的属主不是容器用户。\n"
            f"  修复（在部署机执行，路径换成你实际的数据目录）：\n"
            f"{fix}\n"
            f"  若用的是命名卷，可改为：\n"
            f"    docker run --rm -v competitive-board-data:/d alpine "
            f"chown -R 10001:10001 /d\n"
            f"  或直接删掉卷重建（会丢数据）：docker compose down && "
            f"docker volume rm competitive-board-data"
        ) from exc

    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return
    if free < 1024 * 1024:
        raise RuntimeError(
            f"数据目录 {path} 所在分区仅剩 {free} 字节，SQLite 无法写入"
        )


def init_db() -> None:
    """建表 → 升版本。两步缺一不可：

    - `create_all` 只补「缺的表」，不会改已有表，对新库足够；
    - 对老库（0.7.x 及更早），字段与索引的演进必须走 schema.migrate，
      否则升级镜像后数据卷仍是旧结构，查询时才报 no such column。
    """
    if _is_sqlite:
        _diagnose_data_dir(config.DATA_DIR)
        # WAL 只需设置一次：它是持久属性。而且 PRAGMA journal_mode 不能在事务里执行
        # （BEGIN IMMEDIATE 之后会报 cannot change into wal mode），所以走裸连接。
        raw = engine.raw_connection()
        try:
            raw.cursor().execute("PRAGMA journal_mode=WAL")
        finally:
            raw.close()
    # 规格书原文目录：SQLite 模式下上面已确认可写；PostgreSQL 模式下目录不可写
    # 不致命（只影响规格书上传），不该因此拒绝启动，所以这里吞掉异常。
    try:
        (config.DATA_DIR / "datasheets").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    SQLModel.metadata.create_all(engine)
    result = schema.migrate(engine)
    if result["applied"]:
        log.info(
            "表结构 %d → %d：%s",
            result["from"],
            result["to"],
            "；".join(result["applied"]),
            extra={"event": "schema_migrate"},
        )


# 进程级写锁：SQLite 同时只允许一个写事务。默认的「先读后写」事务在 WAL 下
# 要把读锁升级成写锁，若这期间别的连接写过库，SQLite 会**立刻**抛
# SQLITE_BUSY_SNAPSHOT（"database is locked"），busy_timeout 不管这种情况。
# 后台定时抓取写库时，前台登录/保存偶发 500 就是这么来的（v1.1.1 本地 Docker 冒烟复现）。
# 做法：所有写请求与后台写入串行化（读请求不受影响），冲突改为排队等待而不是失败。
#
# 必须是普通 Lock 而不是 RLock：FastAPI 的 yield 依赖「取」和「放」可能落在
# 线程池的不同线程上，而 RLock 有线程归属，跨线程 release 会抛
# RuntimeError: cannot release un-acquired lock —— 锁一旦漏放，后续写请求全卡死
# （本地压测里表现为登录请求 120 秒超时）。普通 Lock 允许任意线程释放。
WRITE_LOCK = threading.Lock()


def get_session(request: Request) -> Iterator[Session]:
    """只对 SQLite 的写请求串行化；PostgreSQL 不需要也不会被拖慢。

    等待带 30 秒上限：锁再拥挤也不该让请求无限期挂着。真拿不到就照常执行，
    由 SQLite 自己的 busy_timeout 兜底 —— 这里的写事务都很短，正常不会走到那条路。
    """
    write = IS_SQLITE and request.method not in ("GET", "HEAD", "OPTIONS")
    acquired = WRITE_LOCK.acquire(timeout=30) if write else False
    try:
        with Session(engine) as session:
            yield session
    finally:
        if acquired:
            WRITE_LOCK.release()