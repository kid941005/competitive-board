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
from .models import SpecSource

DOC_URL_RE = re.compile(r"^/datasheet/([0-9a-f]{12})/file$")


def referenced_doc_ids(session: Session) -> set[str]:
    """当前被任何一条参数引用着的 doc_id。"""
    urls = session.exec(
        select(SpecSource.url).where(SpecSource.url.like("/datasheet/%"))  # type: ignore[attr-defined]
    ).all()
    out: set[str] = set()
    for url in urls:
        match = DOC_URL_RE.match(url or "")
        if match:
            out.add(match.group(1))
    return out


def _uploaded_at(meta_path: Path) -> datetime:
    """优先用元数据里的上传时间，读不到再退回文件 mtime。"""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        stamp = meta.get("uploaded_at")
        if stamp:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, TypeError):
        pass
    return datetime.fromtimestamp(meta_path.stat().st_mtime, timezone.utc)


def purge_orphans(session: Session, keep_days: int) -> dict:
    """清理「没被引用且过期」的规格书文件。返回统计，供启动日志。"""
    if keep_days <= 0 or not DATASHEET_DIR.exists():
        return {"removed": 0, "kept": 0, "referenced": 0, "bytes": 0}

    referenced = referenced_doc_ids(session)
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed = kept = refs = freed = 0

    for meta_path in sorted(DATASHEET_DIR.glob("*.json")):
        doc_id = meta_path.stem
        if doc_id in referenced:
            refs += 1
            continue
        if _uploaded_at(meta_path) >= cutoff:
            kept += 1
            continue

        # 元数据里记着扩展名；读不到就把同 doc_id 的文件都删掉
        try:
            ext = json.loads(meta_path.read_text(encoding="utf-8")).get("ext") or ""
        except (OSError, ValueError):
            ext = ""
        targets = [meta_path]
        if ext:
            targets.append(DATASHEET_DIR / f"{doc_id}{ext}")
        else:
            targets.extend(p for p in DATASHEET_DIR.glob(f"{doc_id}.*") if p != meta_path)

        deleted = False
        for path in targets:
            try:
                if path.exists():
                    freed += path.stat().st_size
                    path.unlink()
                    deleted = True
            except OSError:
                continue
        if deleted:
            removed += 1

    return {"removed": removed, "kept": kept, "referenced": refs, "bytes": freed}
