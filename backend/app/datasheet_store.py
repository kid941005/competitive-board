"""规格书原文的保留与清理（F-09）。

问题：每次上传都落一份 `<doc_id>.<ext>` + `<doc_id>.json` 到 `DATA_DIR/datasheets`，
从来没有删除路径。100 个型号 × 多次重传 × 每份几 MB，一年下来是几个 GB 的无主文件，
而且它藏在 Docker 卷里，最容易被忽略。

保留策略（按「有没有人引用」判定，而不是只看时间）：

- **被引用就永久保留**：`SpecSource.url == /datasheet/<doc_id>/file` 说明这个文件
  是某条参数的唯一出处，删了溯源就断了；
- **没被引用**：上传了但没提交复核——保留 `datasheet.keep_days` 天（默认 30），
  够用户回头再看一眼；超期才删；
- 删除时 `.json` 与原文一起删，不留半对孤儿文件。

清理时机：启动时（与 audit.purge 同一个位置）。容器每次升级/重启都会执行一次，
对「一年几个 GB」这个量级足够；需要立刻回收时重启一次即可。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, select

from .config import DATASHEET_DIR
from .models import Adoption, ProductSpec, SpecSource

DOC_URL_RE = re.compile(r"^/datasheet/([0-9a-f]{12})/file$")
DOC_FILE_RE = re.compile(r"^([0-9a-f]{12})(?:\.[A-Za-z0-9]+)?$")


def referenced_doc_ids(session: Session) -> set[str]:
    """当前仍被参数或客户采用记录引用的 doc_id。

    只看 SpecSource.url 会把「来源行还在、但已没有任何业务记录引用它」的
    文档误判为在用，导致文件永久保留。来源行与业务记录的生命周期不同：
    删除型号后会先回收无引用来源，这里必须按实际引用计数判断。
    """
    spec_source_ids = session.exec(select(ProductSpec.source_id)).all()
    adoption_source_ids = session.exec(select(Adoption.source_id)).all()
    active_source_ids = {
        source_id for source_id in (*spec_source_ids, *adoption_source_ids)
        if source_id is not None
    }
    if not active_source_ids:
        return set()
    urls = session.exec(
        select(SpecSource.url).where(
            SpecSource.id.in_(active_source_ids),  # type: ignore[attr-defined]
            SpecSource.url.like("/datasheet/%"),  # type: ignore[attr-defined]
        )
    ).all()
    out: set[str] = set()
    for url in urls:
        match = DOC_URL_RE.match(url or "")
        if match:
            out.add(match.group(1))
    return out


def _uploaded_at(paths: list[Path], meta_path: Path | None) -> datetime | None:
    """优先用元数据里的上传时间；元数据坏了就保守地取现存文件的最新 mtime。"""
    if meta_path is not None:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            stamp = meta.get("uploaded_at")
            if stamp:
                parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    mtimes: list[float] = []
    for path in paths:
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), timezone.utc)


def _group_documents() -> dict[str, list[Path]]:
    """按 doc_id 汇总原文与元数据；没有 .json 的原文也必须纳入清理。"""
    groups: dict[str, list[Path]] = {}
    try:
        entries = list(DATASHEET_DIR.iterdir())
    except OSError:
        return groups
    for path in entries:
        match = DOC_FILE_RE.fullmatch(path.name)
        try:
            if match and path.is_file():
                groups.setdefault(match.group(1), []).append(path)
        except OSError:
            continue
    return groups


def purge_orphans(session: Session, keep_days: int) -> dict:
    """清理「没被引用且过期」的规格书文件。返回统计，供启动日志。"""
    # 目录可能在启动扫描时被外部清理工具删除或暂时不可读；清理失败不能
    # 阻断应用启动，也不能把未知状态误判成已经完成清理。
    try:
        entries = list(DATASHEET_DIR.iterdir())
    except OSError:
        return {"removed": 0, "kept": 0, "referenced": 0, "bytes": 0}

    # 原子写入使用 <doc_id>.<ext>.tmp / <doc_id>.json.tmp。进程在两次 replace
    # 之间崩溃时，它们不会被正式文件正则扫到，会永久占空间。临时文件没有
    # 「未提交复核」的保留价值，因此无论 keep_days 是否为 0 都清掉。
    removed_temps = freed_temps = 0
    for path in entries:
        try:
            if path.is_file() and path.name.endswith(".tmp"):
                freed_temps += path.stat().st_size
                path.unlink()
                removed_temps += 1
        except FileNotFoundError:
            continue
        except OSError:
            continue

    if keep_days <= 0:
        return {
            "removed": removed_temps, "kept": 0, "referenced": 0, "bytes": freed_temps,
        }

    referenced = referenced_doc_ids(session)
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed = removed_temps
    kept = refs = 0
    freed = freed_temps

    for doc_id, paths in sorted(_group_documents().items()):
        if doc_id in referenced:
            refs += 1
            continue
        meta_path = next((p for p in paths if p.suffix.lower() == ".json"), None)
        uploaded_at = _uploaded_at(paths, meta_path)
        # 连文件时间都读不到时宁可留下：清理不能让未知状态变成永久丢数据。
        if uploaded_at is None or uploaded_at >= cutoff:
            kept += 1
            continue

        deleted = False
        for path in paths:
            try:
                size = path.stat().st_size
                path.unlink()
                freed += size
                deleted = True
            except FileNotFoundError:
                continue
            except OSError:
                continue
        if deleted:
            removed += 1

    return {"removed": removed, "kept": kept, "referenced": refs, "bytes": freed}
