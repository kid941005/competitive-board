"""市场消息：入库、翻页查询、定时刷新。

刷新一个源 = 抓 → 解析 → 按 dedupe_key 去重 → 只插新条目 → 回写源状态。

几个刻意的取舍：

- **一个源失败不影响别的源**。每个源单独 commit，抓取失败只落一行错误状态；
  某个站点改版或临时 502，不该让整个看板停更。
- **条件请求优先**。带上 etag / last_modified，源没更新时回 304，连解析都省了。
  这是喂 20 个源还不被当成爬虫打的关键。
- **翻页用游标而不是 offset**。时间线是倒序的，新条目随时插到最前面，
  offset 分页会重复或漏项；游标 (published_at, id) 与 ix_news_items_published 对齐。
- **不覆盖已有条目**。同一 dedupe_key 只留最早那条：源改标题/改摘要属于噪声，
  重写反而会让用户看到「标题自己变了」。
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import func, or_
from sqlmodel import Session, select

from . import news_link, settings_store
from .db import IS_SQLITE, WRITE_LOCK, engine


@contextmanager
def _write_lock():
    """入库那一下才拿进程写锁；只对 SQLite 有意义（PostgreSQL 并发写没这个问题）。"""
    if IS_SQLITE:
        WRITE_LOCK.acquire()
    try:
        yield
    finally:
        if IS_SQLITE:
            WRITE_LOCK.release()
from .models import Entity, NewsItem, NewsSource, utcnow
from .news_fetch import (
    DEFAULT_TIMEOUT,
    FeedError,
    FeedItem,
    FetchResult,
    dedupe_key,
    fetch_feed,
    parse_feed,
)
from .timeutil import fmt_local, iso_utc, to_local

DEFAULT_REFRESH_MINUTES = 60
STARTUP_DELAY_SECONDS = 20
ERROR_LIMIT = 500
PAGE_SIZE_DEFAULT = 24
PAGE_SIZE_MAX = 60

Fetcher = Callable[..., Any]

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _day_parts(value: datetime | None) -> tuple[str, str]:
    """日期分组键与中文标题："2026-11-27" / "11月27日 星期五"。

    为什么按本地时区分组而不是按 UTC 日期：22:00 UTC 的消息在北京时间是次日 06:00，
    按 UTC 分组会把它排到前一天的组里，用户看到的日期就和卡片上的时刻对不上。
    """
    local = to_local(value)
    if local is None:
        return "", ""
    return local.strftime("%Y-%m-%d"), f"{local.month}月{local.day}日 {_WEEKDAYS[local.weekday()]}"


def group_by_day(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把时间倒序的条目切成按天的分组，供模板渲染日期头。

    分组必须保持原顺序：条目已经是倒序的，相邻同一天合并即可，
    不能先按天排序再组 —— 那样组内顺序会乱。
    """
    groups: list[dict[str, Any]] = []
    for item in items:
        if not groups or groups[-1]["day"] != item["day"]:
            groups.append({"day": item["day"], "label": item["day_label"], "items": []})
        groups[-1]["items"].append(item)
    for group in groups:
        group["count"] = len(group["items"])
    return groups


# ── 入库 ────────────────────────────────────────────────────────────────────
def _fetch_entries(
    url: str,
    etag: str | None,
    last_modified: str | None,
    *,
    fetch: Fetcher,
    timeout: float,
) -> tuple[FetchResult, list[FeedItem]]:
    """网络请求 + 解析，全部在**事务之外**完成。

    为什么要单独拆出来：SQLite 是单写库，事务期间持有写锁。若把一次最长 10s 的
    网络等待夹在事务里，期间所有写请求（登录、保存）都得排队，用户看到的是页面卡住。
    """
    result = fetch(url, timeout=timeout, etag=etag, last_modified=last_modified)
    if result.not_modified:
        return result, []
    return result, parse_feed(result.body, base_url=url)


def _apply_entries(
    session: Session,
    source: NewsSource,
    result: FetchResult,
    entries: list[FeedItem],
    now: datetime,
) -> int:
    """把一次抓取结果写进库。不 commit。返回新增条数。"""
    if result.not_modified:
        # 304：源没更新。这不是失败，状态记 ok 并清掉上次的错误
        source.last_status = "ok"
        source.last_error = None
        return 0
    source.etag = result.etag
    source.last_modified = result.last_modified
    added = _store_items(session, source, entries, now)
    source.last_status = "ok" if entries else "empty"
    source.last_error = None
    source.item_count = _count_source(session, source)
    return added


def refresh_source(
    session: Session,
    source: NewsSource,
    *,
    fetch: Fetcher = fetch_feed,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """抓一个源并入库。不 commit —— 由调用方决定事务边界。"""
    now = utcnow()
    source.last_fetch_at = now
    try:
        result, entries = _fetch_entries(
            source.url, source.etag, source.last_modified, fetch=fetch, timeout=timeout
        )
    except FeedError as exc:
        source.last_status = "error"
        source.last_error = str(exc)[:ERROR_LIMIT]
        return {"source": source.name, "status": "error", "added": 0, "error": str(exc)}

    added = _apply_entries(session, source, result, entries, now)
    return {"source": source.name, "status": source.last_status, "added": added, "error": None}


def _store_items(
    session: Session, source: NewsSource, entries: list[FeedItem], now: datetime
) -> int:
    """按 dedupe_key 批量去重后插入。一次查询判重，避免 N 条 N 次查库。"""
    prepared: list[tuple[FeedItem, str]] = []
    seen: set[str] = set()
    for entry in entries:
        key = dedupe_key(entry.url, source.id or 0, entry.guid)
        if key in seen:  # 同一个源里重复出现的条目，先在本批内去掉
            continue
        seen.add(key)
        prepared.append((entry, key))
    if not prepared:
        return 0

    existing = set(
        session.exec(
            select(NewsItem.dedupe_key).where(NewsItem.dedupe_key.in_([k for _, k in prepared]))
        ).all()
    )
    added = 0
    for entry, key in prepared:
        if key in existing:
            continue
        session.add(
            NewsItem(
                source_id=source.id,
                entity_id=source.entity_id,
                guid=entry.guid,
                url=entry.url,
                title=_clean_title(entry.title, source.url),
                summary=entry.summary,
                lang=source.lang,
                # 源没给时间就用抓取时间：宁可时间偏晚，也不要一条消息沉到时间线底部
                published_at=entry.published_at or now,
                fetched_at=now,
                dedupe_key=key,
            )
        )
        added += 1
    return added


def _clean_title(title: str, source_url: str) -> str:
    """Google News 会在标题后面拼「 - 媒体名」，媒体名我们另有来源，去掉更好读。

    只对 Google News 生效：别的源里「A - B」可能就是标题本身。
    """
    if "news.google.com" not in source_url:
        return title
    head, sep, tail = title.rpartition(" - ")
    if sep and head.strip() and len(tail) <= 40:
        return head.strip()
    return title


def _count_source(session: Session, source: NewsSource) -> int:
    return int(
        session.exec(
            select(func.count()).select_from(NewsItem).where(NewsItem.source_id == source.id)
        ).one()
    )


def refresh_all(session: Session, *, fetch: Fetcher = fetch_feed) -> dict[str, int]:
    """刷新全部启用的源。每个源单独提交，前一个失败不影响后一个。

    网络与解析放在写锁之外，只有入库那一下才拿 WRITE_LOCK —— 否则一次抓取
    就能把全站的写请求堵上十几秒（详见 _fetch_entries 的说明）。
    """
    # 手工补录源没有 feed 地址（url 是 manual://clip），自动抓取要跳过它
    sources = session.exec(
        select(NewsSource)
        .where(NewsSource.enabled.is_(True), NewsSource.kind != "manual")
        .order_by(NewsSource.id)
    ).all()
    plan = [(s.id, s.url, s.etag, s.last_modified) for s in sources]
    session.commit()  # 结束只读事务，别把它带到网络等待里
    stats = {"sources": len(sources), "added": 0, "failed": 0}
    for source_id, url, etag, last_modified in plan:
        try:
            result, entries = _fetch_entries(
                url, etag, last_modified, fetch=fetch, timeout=DEFAULT_TIMEOUT
            )
        except FeedError as exc:
            with _write_lock():
                source = session.get(NewsSource, source_id)
                if source is None:
                    continue
                source.last_fetch_at = utcnow()
                source.last_status = "error"
                source.last_error = str(exc)[:ERROR_LIMIT]
                session.commit()
            stats["failed"] += 1
            continue
        with _write_lock():
            source = session.get(NewsSource, source_id)
            if source is None:
                continue
            added = _apply_entries(session, source, result, entries, utcnow())
            session.commit()
        stats["added"] += added
        if source.last_status == "error":
            stats["failed"] += 1
    return stats


def refresh_all_once() -> dict[str, int]:
    """给后台定时任务用的同步入口：自己开 session。"""
    with Session(engine) as session:
        return refresh_all(session)


_REFRESH_GUARD = threading.Lock()


def refresh_background() -> bool:
    """从请求里触发全量刷新：开独立线程跑，不挂在请求作用域里。

    用 BackgroundTasks 会踩一个隐蔽的死锁：FastAPI 的依赖清理在后台任务之后才执行，
    后台任务里的写锁要等请求结束才释放（本地 Docker 压测复现：点一次刷新后，
    紧随其后的登录卡 40 秒以上）。独立线程 + 非阻塞闸门，重复点击直接忽略。
    """
    if not _REFRESH_GUARD.acquire(blocking=False):
        return False

    def _run() -> None:
        try:
            stats = refresh_all_once()
            print(
                f"[news] 手动刷新：{stats['sources']} 个源，新增 {stats['added']} 条，"
                f"失败 {stats['failed']} 个",
                flush=True,
            )
            # 新进来的 Google News 条目顺手在后台换成原文地址
            news_link.backfill_async()
        except Exception as exc:  # 后台线程异常不能把进程带走
            print(f"[news] 手动刷新异常：{exc}", flush=True)
        finally:
            _REFRESH_GUARD.release()

    threading.Thread(target=_run, name="news-refresh", daemon=True).start()
    return True


def seed_builtin(session: Session, specs: list[dict[str, Any]]) -> int:
    """空库时预置内置源。已有任何源就不动 —— 用户删掉的源不该被重启塞回来。

    spec 里的 entity 是公司名，这里解析成 entity_id：官方源和检索源都挂到同一家，
    时间线按公司筛选时才不会一半有一半没有。
    """
    if session.exec(select(func.count()).select_from(NewsSource)).one():
        return 0
    entities = session.exec(select(Entity)).all()
    by_name = {e.name: e.id for e in entities}
    by_name_en = {e.name_en: e.id for e in entities if e.name_en}
    added = 0
    for spec in specs:
        data = dict(spec)
        company = data.pop("entity", None)
        if company:
            data["entity_id"] = by_name.get(company) or by_name_en.get(company)
        session.add(NewsSource(**data))
        added += 1
    session.commit()
    return added


# ── 查询 ────────────────────────────────────────────────────────────────────
def encode_cursor(published_at: datetime, item_id: int) -> str:
    """游标 = 「发布时间(微秒时间戳)|id」。

    刻意用整数而不是 ISO 串：ISO 里的 '+' 在查询串里会被解成空格，
    任何没做 URL 转义的调用方（curl、脚本、手拼链接）都会静默拿到第一页。
    """
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return f"{int(published_at.timestamp() * 1_000_000)}|{item_id}"


def decode_cursor(raw: str | None) -> tuple[datetime, int] | None:
    """坏游标（手工改过、跨版本失效）当作从头开始，不报错。"""
    if not raw:
        return None
    stamp, _, tail = raw.rpartition("|")
    try:
        value = datetime.fromtimestamp(int(stamp) / 1_000_000, tz=timezone.utc)
        item_id = int(tail)
    except (ValueError, OverflowError, OSError):
        return None
    return value, item_id


def _filters(
    entity_id: int | None, source_id: int | None, keyword: str | None
) -> list[Any]:
    clauses: list[Any] = []
    if entity_id:
        clauses.append(NewsItem.entity_id == entity_id)
    if source_id:
        clauses.append(NewsItem.source_id == source_id)
    text = (keyword or "").strip()
    if text:
        # autoescape：用户搜 "100%" 不该被当成通配符
        clauses.append(
            or_(
                NewsItem.title.contains(text, autoescape=True),
                NewsItem.summary.contains(text, autoescape=True),
            )
        )
    return clauses


def query_items(
    session: Session,
    *,
    entity_id: int | None = None,
    source_id: int | None = None,
    keyword: str | None = None,
    cursor: str | None = None,
    limit: int = PAGE_SIZE_DEFAULT,
) -> dict[str, Any]:
    """时间倒序取一页。返回 items / next_cursor / total。"""
    size = max(1, min(int(limit or PAGE_SIZE_DEFAULT), PAGE_SIZE_MAX))
    clauses = _filters(entity_id, source_id, keyword)
    after = decode_cursor(cursor)

    stmt = (
        select(NewsItem, NewsSource.name, NewsSource.url, Entity.name)
        .join(NewsSource, NewsItem.source_id == NewsSource.id)
        .outerjoin(Entity, NewsItem.entity_id == Entity.id)
    )
    for clause in clauses:
        stmt = stmt.where(clause)
    if after is not None:
        stamp, item_id = after
        stmt = stmt.where(
            or_(
                NewsItem.published_at < stamp,
                (NewsItem.published_at == stamp) & (NewsItem.id < item_id),
            )
        )
    stmt = stmt.order_by(NewsItem.published_at.desc(), NewsItem.id.desc()).limit(size + 1)

    rows = session.exec(stmt).all()
    has_more = len(rows) > size
    rows = rows[:size]
    items = [
        {
            "id": item.id,
            "title": item.title,
            "url": item.url,
            "real_url": item.real_url,
            "summary": item.summary,
            "lang": item.lang,
            "published_at": item.published_at,
            # 时间在库里是 UTC，展示前先转本地：模板只负责摆位置，不做时区运算
            "published_hm": fmt_local(item.published_at, "%H:%M"),
            "published_label": fmt_local(item.published_at, "%m-%d %H:%M"),
            "published_full": fmt_local(item.published_at, "%Y-%m-%d %H:%M"),
            "published_iso": iso_utc(item.published_at) or "",
            "source_name": source_name,
            "source_url": source_url,
            "entity_name": entity_name,
        }
        for item, source_name, source_url, entity_name in rows
    ]
    # 日期分组键在这里补，模板只负责排版
    for item in items:
        item["day"], item["day_label"] = _day_parts(item["published_at"])
        # 「原文」要跳真实文章：Google News 的链接是跳转页，解析过的直接用原文地址，
        # 还没解析的走站内 /news/item/{id}/go 现解析（约 1~3 秒）并缓存。
        item["link"] = item["real_url"] or (
            f"/news/item/{item['id']}/go"
            if news_link.is_google_news(item["url"])
            else (item["url"] or "")
        )
    next_cursor = None
    if has_more and rows:
        last = rows[-1][0]
        next_cursor = encode_cursor(last.published_at, int(last.id or 0))

    total_stmt = select(func.count()).select_from(NewsItem)
    for clause in clauses:
        total_stmt = total_stmt.where(clause)
    total = int(session.exec(total_stmt).one())
    return {"items": items, "next_cursor": next_cursor, "total": total}


def filter_options(session: Session) -> dict[str, Any]:
    """筛选项：有消息的公司 + 有消息的源。空公司不占位置。"""
    entities = session.exec(
        select(Entity)
        .where(Entity.kind.in_(("competitor", "customer", "self")))
        .order_by(Entity.kind, Entity.name)
    ).all()
    used = set(
        session.exec(select(NewsItem.entity_id).where(NewsItem.entity_id.is_not(None))).all()
    )
    sources = session.exec(
        select(NewsSource, func.count(NewsItem.id))
        .join(NewsItem, NewsItem.source_id == NewsSource.id, isouter=True)
        .group_by(NewsSource.id)
        .order_by(NewsSource.name)
    ).all()
    return {
        "entities": [e for e in entities if e.id in used],
        "sources": [
            {"id": src.id, "name": src.name, "count": int(count or 0)}
            for src, count in sources
        ],
    }


# ── 定时刷新 ────────────────────────────────────────────────────────────────
def _refresh_minutes() -> int:
    """0 = 关闭自动刷新。参数改完下一轮生效，不需要重启。"""
    with Session(engine) as session:
        value = settings_store.all_values(session).get(
            "news.refresh_minutes", DEFAULT_REFRESH_MINUTES
        )
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_MINUTES


async def _scheduler_loop() -> None:
    """启动后延迟一小会儿再抓第一轮：让健康检查、数据库就绪先过去。"""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        minutes = _refresh_minutes()
        if minutes <= 0:
            await asyncio.sleep(60)  # 关闭状态每分钟看一眼参数
            continue
        try:
            stats = await asyncio.to_thread(refresh_all_once)
            if stats["added"] or stats["failed"]:
                print(
                    f"[news] 定时刷新：{stats['sources']} 个源，新增 {stats['added']} 条，"
                    f"失败 {stats['failed']} 个",
                    flush=True,
                )
            news_link.backfill_async()
        except Exception as exc:  # 后台任务抛异常不能把进程带走
            print(f"[news] 定时刷新异常：{exc}", flush=True)
        await asyncio.sleep(minutes * 60)


def start_scheduler() -> "asyncio.Task[None]":
    return asyncio.create_task(_scheduler_loop(), name="news-scheduler")


async def stop_scheduler(task: "asyncio.Task[None] | None") -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
