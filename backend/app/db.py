"""数据库引擎与会话。"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

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
    # 规格书原文目录：SQLite 模式下上面已确认可写；PostgreSQL 模式下目录不可写
    # 不致命（只影响规格书上传），不该因此拒绝启动，所以这里吞掉异常。
    try:
        (config.DATA_DIR / "datasheets").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    SQLModel.metadata.create_all(engine)
    result = schema.migrate(engine)
    if result["applied"]:
        print(f"[db] 表结构 {result['from']} → {result['to']}：" + "；".join(result["applied"]),
              flush=True)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
