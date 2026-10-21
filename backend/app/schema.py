"""表结构与迁移。

为什么不用 Alembic：本项目的迁移只有三类动作 —— 建索引、补列、修数据，没有分支、
没有 downgrade 需求，却要同时跑在 SQLite 与 PostgreSQL 上。为此引入 Alembic 的
`env.py` / `alembic.ini` / `versions/` 三层配置，收益不抵成本；而真正必须做的事
（存量重复数据先合并、再建唯一索引）在两边都得手写。所以这里用一个约 150 行的
顺序迁移器，规则与 Alembic 一致：

- `schema_meta.version` 记录已应用到第几版；
- 每一步在自己的事务里执行，失败即回滚，不会留下半套结构；
- **库比代码新就拒绝启动** —— 老镜像配新库字段，等到查询时才报 `no such column`
  是最难查的一类故障；
- 每一步都必须幂等（`IF NOT EXISTS` / 先查后建），重复执行不出错。

唯一性放在数据库，而不是只靠代码：`Product(entity_id, lower(model))` 与
`ProductSpec(product_id, metric_id)` 一旦重复，`build_matrix` 会静默取后者，
而两处并发写入都能成功。索引是最后一道兜底。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session, select

from . import audit
from .models import Product, ProductSpec, SpecMetric

# 当前代码期望的表结构版本。加一版就在这里改一个数字，并在 MIGRATIONS 末尾追加一步。
CURRENT_SCHEMA_VERSION = 4

_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   VARCHAR NOT NULL PRIMARY KEY,
    value VARCHAR NOT NULL
)
"""

# 产品型号在「同一家公司内」唯一；比较时忽略大小写与首尾空白。
# 表达式索引，SQLite 3.9+ 与 PostgreSQL 都支持。
UX_PRODUCT = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_products_entity_model "
    "ON products (entity_id, lower(trim(model)))"
)
# 一个 (型号, 指标) 只能有一格。重复时矩阵会按字典覆盖，静默丢掉其中一条。
UX_SPEC = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_specs_product_metric "
    "ON product_specs (product_id, metric_id)"
)
# 同一家客户对同一款泵只记一条采用关系（状态变了就更新那条，不并列两条）。
UX_ADOPTION = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_adoptions_customer_product "
    "ON adoptions (customer_id, product_id)"
)


# ── 迁移步骤 ───────────────────────────────────────────────────────────────
def _v1_baseline(session: Session) -> None:
    """基线：建表交给 SQLModel.metadata.create_all（已在本函数之前执行）。

    这一步只做标记，让「0.7.x 老库」与「全新库」站在同一条起跑线上。
    """


def _dedupe_products(session: Session) -> list[str]:
    """按 (公司, 归一型号) 合并重复型号。返回被合并的说明，供审计记录。

    合并规则（宁可保守）：
    - 保留参数最多的那一条（并列取 id 最小，即最早建档的）；
    - 其余各条把「赢家没有的指标」迁过去，冲突的格子保留赢家的值；
    - 迁完删除输家。整个过程只发生在同公司同型号之间，不影响其他数据。
    """
    rows = session.exec(select(Product)).all()
    groups: dict[tuple[int, str], list[Product]] = {}
    for p in rows:
        key = (p.entity_id, re.sub(r"\s+", "", (p.model or "")).lower())
        groups.setdefault(key, []).append(p)

    notes: list[str] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        specs = {
            p.id: session.exec(
                select(ProductSpec).where(ProductSpec.product_id == p.id)
            ).all()
            for p in group
        }
        group.sort(key=lambda p: (-len(specs[p.id]), p.id or 0))
        winner, losers = group[0], group[1:]
        held = {s.metric_id for s in specs[winner.id]}
        for loser in losers:
            moved = dropped = 0
            for s in specs[loser.id]:
                if s.metric_id in held:
                    session.delete(s)  # 赢家已有该指标，冲突格以赢家为准
                    dropped += 1
                else:
                    s.product_id = winner.id
                    session.add(s)
                    held.add(s.metric_id)
                    moved += 1
            session.flush()
            session.delete(loser)
            session.flush()
            notes.append(
                f"合并重复型号 {loser.model}（id={loser.id} → id={winner.id}，"
                f"迁入 {moved}、丢弃 {dropped}）"
            )
        # 型号名有大小写/空白差异时，统一成赢家的写法
        audit.record(
            session, actor="system", actor_kind="system", action="update",
            object_type="product", object_id=winner.id, label=winner.model,
            origin="migration",
            changes=[{"field": "重复型号合并", "old": f"{len(losers) + 1} 条", "new": "1 条"}],
            note="；".join(notes[-len(losers):]),
        )
    return notes


def _dedupe_specs(session: Session) -> int:
    """同一 (型号, 指标) 只留一行：留最近更新的一条（并列取 id 最大）。"""
    rows = session.exec(select(ProductSpec)).all()
    groups: dict[tuple[int, int], list[ProductSpec]] = {}
    for s in rows:
        groups.setdefault((s.product_id, s.metric_id), []).append(s)

    dropped = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda s: (s.updated_at, s.id or 0), reverse=True)
        for extra in group[1:]:
            session.delete(extra)
            dropped += 1
    if dropped:
        session.flush()
        audit.record(
            session, actor="system", actor_kind="system", action="delete",
            object_type="spec", label="重复参数",
            origin="migration",
            changes=[{"field": "重复格", "old": str(dropped), "new": "0"}],
            note=f"同一型号的同一指标存在多行，保留最近更新的一条，删除 {dropped} 行",
        )
    return dropped


def _v2_unique_indexes(session: Session) -> None:
    """先合并重复数据，再建唯一索引。

    顺序不能反：存量库里只要有一对重复，CREATE UNIQUE INDEX 就会失败，
    而失败发生在启动阶段，用户看到的是「服务起不来」而不是「有两条重复」。
    """
    _dedupe_specs(session)
    _dedupe_products(session)
    conn = session.connection()
    conn.execute(text(UX_PRODUCT))
    conn.execute(text(UX_SPEC))


def _v3_merge_gas_metrics(session: Session) -> None:
    """把「风量 / 静压(风扇)」并进「空气流量 / 静压(气泵)」，删掉「真空度」。

    起因是跨大类对比：选一个气泵 + 一个风扇，表里会出现两行流量、两行静压 ——
    物理量明明是同一个，只因为指标字典按大类各建了一套。合并后每个物理量只有一行。

    细节：
    - 风扇的静压原单位是 Pa，气泵是 kPa，迁移时 ÷1000（同一物理量，不是近似）。
    - 两边都有值的型号（理论上不该有），保留目标指标那一格，源那一格删掉并记录条数。
    - 「真空度」全库只有村田一条，且没有任何规格书原文出现过，直接删掉指标与数据。
    - 每步先查再动，重复执行不出错（迁移器要求幂等）。
    """
    metrics = {m.key: m for m in session.exec(select(SpecMetric)).all()}
    merged: list[str] = []

    for src_key, dst_key, factor, unit_note in (
        ("flow_fan", "flow_air", 1.0, "L/min"),
        ("pressure_fan", "pressure_air", 0.001, "Pa → kPa"),
    ):
        src, dst = metrics.get(src_key), metrics.get(dst_key)
        if src is None or dst is None:
            continue

        if "fan" not in (dst.applies_to or "").split(","):
            dst.applies_to = ",".join(x for x in [(dst.applies_to or ""), "fan"] if x)
            session.add(dst)

        moved = dropped = 0
        specs = session.exec(
            select(ProductSpec).where(ProductSpec.metric_id == src.id)
        ).all()
        for spec in specs:
            value = spec.value_num * factor if spec.value_num is not None else None
            existing = session.exec(
                select(ProductSpec).where(
                    ProductSpec.product_id == spec.product_id,
                    ProductSpec.metric_id == dst.id,
                )
            ).first()
            if existing is not None:
                session.delete(spec)
                dropped += 1
                continue
            spec.metric_id = dst.id
            if value is not None:
                spec.value_num = value
            session.add(spec)
            moved += 1
        session.flush()
        session.delete(src)
        merged.append(f"{src_key} → {dst_key}（迁 {moved} 条、丢弃重复 {dropped} 条，{unit_note}）")

    vac = metrics.get("vacuum_air")
    if vac is not None:
        dropped = 0
        for spec in session.exec(
            select(ProductSpec).where(ProductSpec.metric_id == vac.id)
        ).all():
            session.delete(spec)
            dropped += 1
        session.delete(vac)
        merged.append(f"vacuum_air 删除（连带 {dropped} 条数据）")

    session.flush()
    if merged:
        audit.record(
            session, actor="system", actor_kind="system", action="update",
            object_type="metric", label="指标字典", origin="migration",
            changes=[{"field": "指标合并", "old": "按大类各一套", "new": "流量/静压各一项"}],
            note="；".join(merged),
        )


def _v3_adoption_index(session: Session) -> None:
    """客户采用关系的唯一索引（表本身由 create_all 建）。"""
    _v3_merge_gas_metrics(session)
    conn = session.connection()
    conn.execute(text(UX_ADOPTION))


# v4：把这四个指标放宽到覆盖全部三个大类。
# 依据是业务方实际登记的数据 —— 风扇与液泵同样有谐振频率、温度上限、寿命、噪音，
# 原来只标了 pump_air（噪音到 fan），于是导入时被「不适用于该大类」整格拦掉。
_WIDEN_APPLIES = {
    "resonance": "pump_air,pump_liquid,fan",
    "op_temp_max": "pump_air,pump_liquid,fan",
    "lifetime_h": "pump_air,pump_liquid,fan",
    "noise_dba": "pump_air,pump_liquid,fan",
}


def _v4_widen_applies_to(session: Session) -> None:
    """把通用的驱动/可靠性指标放宽到三个大类（只改 applies_to，不动数值）。"""
    changed: list[str] = []
    for metric in session.exec(select(SpecMetric)).all():
        target = _WIDEN_APPLIES.get(metric.key)
        if target and metric.applies_to != target:
            changed.append(f"{metric.key}: {metric.applies_to} → {target}")
            metric.applies_to = target
            session.add(metric)
    session.flush()
    if changed:
        audit.record(
            session, actor="system", actor_kind="system", action="update",
            object_type="metric", label="指标字典", origin="migration",
            changes=[{"field": "适用大类", "old": "按气泵限定", "new": "气泵+液泵+风扇"}],
            note="；".join(changed),
        )


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Session], None]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline（建表由 create_all 完成）", _v1_baseline),
    Migration(2, "型号与参数加唯一索引（先合并存量重复）", _v2_unique_indexes),
    Migration(3, "合并风量/静压两套指标、删掉真空度、客户采用关系唯一索引", _v3_adoption_index),
    Migration(4, "谐振频率/温度/寿命/噪音放宽到三个大类", _v4_widen_applies_to),
)


# ── 运行器 ─────────────────────────────────────────────────────────────────
class SchemaTooNew(RuntimeError):
    """库的表结构比当前代码新：说明镜像回退了。"""


def read_version(conn) -> int:
    row = conn.execute(
        text("SELECT value FROM schema_meta WHERE key = 'version'")
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _write_version(conn, version: int) -> None:
    conn.execute(
        text(
            "INSERT INTO schema_meta (key, value) VALUES ('version', :v) "
            "ON CONFLICT (key) DO UPDATE SET value = :v"
        ),
        {"v": str(version)},
    )


def migrate(engine) -> dict:
    """把库升级到 CURRENT_SCHEMA_VERSION。返回 {"from": n, "to": m, "applied": [..]}。

    全部步骤在**一个事务**里完成：要么整库升到目标版本，要么完全不动。
    启动期间的并发只有一个进程（部署是 `--workers 1`），因此不需要额外的锁。
    """
    applied: list[str] = []
    with engine.begin() as conn:
        conn.execute(text(_VERSION_TABLE))
        current = read_version(conn)
        if current > CURRENT_SCHEMA_VERSION:
            raise SchemaTooNew(
                f"数据库结构版本 {current} 高于本镜像支持的 {CURRENT_SCHEMA_VERSION}。"
                "请升级镜像（或回退到与之匹配的版本），不要用旧代码打开新库。"
            )
        if current == CURRENT_SCHEMA_VERSION:
            return {"from": current, "to": current, "applied": []}

        # 步骤跑在 savepoint 里：单步失败只回滚这一步，且不会提前提交外层事务。
        # 每步结束必须 session.commit()（释放 savepoint，把改动并入外层事务）——
        # 只 close 不 commit 会把这一步的 DDL 一起回滚掉（踩过一次：
        # 索引建了、版本号写了，事务提交后索引却不见了）。
        session = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            for step in MIGRATIONS:
                if step.version <= current:
                    continue
                step.apply(session)
                session.commit()
                _write_version(conn, step.version)
                applied.append(f"v{step.version} {step.name}")
        finally:
            session.close()
    return {"from": current, "to": CURRENT_SCHEMA_VERSION, "applied": applied}
