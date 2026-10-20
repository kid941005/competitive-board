"""审计日志：记录「谁把什么改成了什么」。

三条设计取舍：

1. **记旧值，不只记动作**。只写「admin 更新了 MZB1001T02」在追查时毫无用处；
   必须能回答「改成多少了」「原来是多少」「谁改的」。
2. **删除留快照**。删掉的数据如果不留一份，事后无法解释丢了什么，也无法恢复判断。
3. **一次请求一个 request_id**。批量改 20 个指标会产生多行，页面上要能折叠成一次操作。

调用方式统一为：改动前 `snapshot_*()` 取快照 → 改 → `record()` 写差异。
不要试图在 ORM 事件里做 —— 那个层面的「旧值」已经被覆盖了。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from .models import AuditLog, Product, ProductSpec, SpecMetric

# 单条日志最多存多少项差异：一次导入几千行时，避免一行日志撑爆
MAX_CHANGES = 200
RETENTION_DEFAULT_DAYS = 180


def _s(value) -> str | None:
    """统一成可比较、可读的字符串。None 表示「没有值」。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        # 去掉浮点尾巴：0.7000000000001 与 0.7 应视为同一个值
        text = f"{value:.10g}"
        return text
    return str(value)


def diff(before: dict, after: dict, labels: dict[str, str] | None = None) -> list[dict]:
    """比较两份快照，只返回真正变化了的字段。"""
    labels = labels or {}
    out: list[dict] = []
    for key in list(before.keys()) + [k for k in after if k not in before]:
        old, new = _s(before.get(key)), _s(after.get(key))
        if old == new:
            continue
        out.append({"field": labels.get(key, key), "old": old, "new": new})
    return out


def product_snapshot(p: Product) -> dict:
    return {
        "model": p.model,
        "name": p.name,
        "category": p.category,
        "status": p.status,
        "datasheet_url": p.datasheet_url,
        "note": p.note,
    }


PRODUCT_LABELS = {
    "model": "型号", "name": "名称", "category": "产品大类",
    "status": "状态", "datasheet_url": "官方链接", "note": "备注",
}


def spec_snapshot(
    session: Session,
    product_id: int,
    metrics: dict[int, SpecMetric] | None = None,
    specs: list[ProductSpec] | None = None,
) -> dict[str, str]:
    """按「指标名」而不是 id 做键：日志是给人看的，id 对不上号。

    specs 可传入：批量写参数时已经一次性取好了，不必每个型号再查一遍。
    """
    if metrics is None:
        metrics = {m.id: m for m in session.exec(select(SpecMetric)).all()}
    if specs is None:
        specs = list(session.exec(
            select(ProductSpec).where(ProductSpec.product_id == product_id)
        ).all())
    out: dict[str, str] = {}
    for s in specs:
        m = metrics.get(s.metric_id)
        if m is None:
            continue
        # 值与条件分两行：只改条件时不应表现为「值从 A 变成 B」
        out[m.name] = _s(s.value_num) if s.value_num is not None else (s.value_text or "")
        if s.condition:
            out[f"{m.name} · 测试条件"] = s.condition
        # 复核状态同样要留痕：它是「有人为这个数字背书」的唯一信号。
        # 只改勾选不改值时，缺了这行 diff 就是空集，record 会直接丢日志。
        out[f"{m.name} · 复核"] = "是" if s.verified else "否"
    return out


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    object_type: str,
    object_id: int | None = None,
    label: str = "",
    changes: list[dict] | None = None,
    origin: str = "web",
    note: str | None = None,
    request_id: str | None = None,
    actor_kind: str = "user",
) -> AuditLog | None:
    """写入一条日志。changes 为空则跳过 —— 没变化就不该产生噪音。"""
    changes = changes or []
    if not changes and action not in ("delete", "import", "login"):
        return None
    row = AuditLog(
        actor=actor or "未知",
        actor_kind=actor_kind,
        action=action,
        object_type=object_type,
        object_id=object_id,
        object_label=(label or "")[:120],
        origin=origin,
        request_id=request_id,
        changes=json.dumps(changes[:MAX_CHANGES], ensure_ascii=False),
        note=(note or None),
    )
    session.add(row)
    return row


# ── 查询 ───────────────────────────────────────────────────────────────────
def parse_changes(row: AuditLog) -> list[dict]:
    try:
        data = json.loads(row.changes or "[]")
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def query(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    actor: str | None = None,
    action: str | None = None,
    object_type: str | None = None,
    origin: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    stmt = select(AuditLog)
    if since:
        stmt = stmt.where(AuditLog.at >= since)
    if until:
        stmt = stmt.where(AuditLog.at <= until)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if object_type:
        stmt = stmt.where(AuditLog.object_type == object_type)
    if origin:
        stmt = stmt.where(AuditLog.origin == origin)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (AuditLog.object_label.ilike(like))  # type: ignore[attr-defined]
            | (AuditLog.changes.ilike(like))  # type: ignore[attr-defined]
            | (AuditLog.note.ilike(like))  # type: ignore[attr-defined]
        )

    total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
    rows = list(session.exec(
        stmt.order_by(AuditLog.at.desc(), AuditLog.id.desc()).offset(offset).limit(limit)
    ).all())
    return rows, int(total or 0)


def distinct_actors(session: Session) -> list[str]:
    rows = session.exec(select(AuditLog.actor).distinct().order_by(AuditLog.actor)).all()
    return [r for r in rows if r]


def purge(session: Session, days: int = RETENTION_DEFAULT_DAYS) -> int:
    """按保留期清理。日志本身会无限增长，必须有出口。"""
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = list(session.exec(select(AuditLog).where(AuditLog.at < cutoff)).all())
    for row in rows:
        session.delete(row)
    if rows:
        session.commit()
    return len(rows)


def log_import(
    session: Session,
    report,
    *,
    actor: str,
    origin: str = "import",
    request_id: str | None = None,
    actor_kind: str = "user",
) -> int:
    """把一次导入的逐项明细写进日志。

    导入是「一次操作改了很多格」的典型场景，所以逐项记：
    只写一行「导入了 37 条」在追查时等于没记。
    """
    details = getattr(report, "details", []) or []
    written = 0
    for item in details[:MAX_CHANGES]:
        audit_row = record(
            session,
            actor=actor,
            actor_kind=actor_kind,
            action="import",
            object_type="spec",
            label=f"{item.get('model', '')} · {item.get('metric', '')}",
            changes=[{
                "field": item.get("metric") or "值",
                "old": item.get("old"),
                "new": item.get("new"),
            }],
            origin=origin,
            request_id=request_id,
        )
        if audit_row is not None:
            written += 1
    if written:
        session.commit()
    return written
