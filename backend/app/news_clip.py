"""手工补录：把一条网页链接（含公众号文章）抓成一条市场消息。

和 RSS 抓取的区别：RSS 有结构化字段，网页只能从 HTML 里猜。
猜的优先级固定写在 _extract 里，猜不到就留空 —— 不编造。

几个刻意的取舍：

- **只取标题 / 一句摘要 / 发布时间**，不抓正文：时间线只需要这些，
  正文涉及版权与体积，想看全文点「原文」跳过去。
- **抓不到发布时间就用抓取时间**：时间线要能倒序，宁可时间偏晚，
  也不要一条消息沉到最底部看不见（与 RSS 抓取口径一致）。
- **不碰任何登录态 / 微信协议**：只抓公开可访问的 HTML 页面，合规风险最低。
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from sqlalchemy import func
from sqlmodel import Session, select

from .models import NewsItem, NewsSource, utcnow
from .news_fetch import (
    SUMMARY_LIMIT,
    TITLE_LIMIT,
    USER_AGENT,
    clean_text,
    normalize_url,
    parse_datetime,
)

# 手工补录的条目挂在一条特殊源上：时间线需要 source_id，且「按来源筛选」时能单独看到
MANUAL_SOURCE_NAME = "手工补录"
MANUAL_SOURCE_URL = "manual://clip"

MAX_URLS = 12          # 每批上限：一条最多 8 秒，再多会让请求等太久
PER_URL_TIMEOUT = 8.0  # 单条抓取超时
TOTAL_BUDGET = 10.0    # 整批抓取总预算，超时的链接直接判失败
PAGE_MAX_BYTES = 2 * 1024 * 1024
URL_LIMIT = 500
ERROR_LIMIT = 300

Fetcher = Callable[..., str]


class ClipError(RuntimeError):
    """单条链接抓不到，或抓回来的不是 HTML 页面。"""


# ── 抓页面 ──────────────────────────────────────────────────────────────────
def _decode(body: bytes, charset: str | None) -> str:
    for encoding in (charset, "utf-8", "gb18030"):
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="ignore")


def _fetch_html(url: str, *, timeout: float = PER_URL_TIMEOUT) -> str:
    if any(ch.isspace() for ch in url):
        # 从微信/文档里复制链接常带不可见空白，直接告诉用户比抛 URLError 好懂
        raise ClipError("链接里有空格或换行，请去掉后再试")
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(PAGE_MAX_BYTES + 1)
            charset = response.headers.get_content_charset()
    except HTTPError as exc:
        raise ClipError(f"抓取失败：HTTP {exc.code}") from exc
    except URLError as exc:
        raise ClipError(f"抓取失败：{exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise ClipError(f"抓取失败：{exc}") from exc
    if len(body) > PAGE_MAX_BYTES:
        raise ClipError(f"页面超过 {PAGE_MAX_BYTES // (1024 * 1024)} MB，跳过")
    return _decode(body, charset)


# ── 从 HTML 里提取字段 ──────────────────────────────────────────────────────
_META_PATTERNS = (
    r"""<meta[^>]+(?:property|name)\s*=\s*["']{key}["'][^>]*?content\s*=\s*["'](?P<value>.*?)["']""",
    r"""<meta[^>]+content\s*=\s*["'](?P<value>.*?)["'][^>]*(?:property|name)\s*=\s*["']{key}["']""",
)

_TITLE_KEYS = ("og:title", "twitter:title")
_SUMMARY_KEYS = ("description", "og:description", "twitter:description")
_DATE_KEYS = ("article:published_time", "og:release_date", "publishdate", "pubdate")

_EPOCH_PATTERNS = (
    r"""var\s+(?:ct|create_time)\s*=\s*["'](\d{9,13})["']""",  # 公众号文章页
    r""""publish_time"\s*:\s*"?(\d{9,13})""",
)
_ISO_PATTERNS = (
    r'"datePublished"\s*:\s*"([^"]+)"',  # JSON-LD
    r"""<time[^>]+datetime\s*=\s*["']([^"']+)["']""",
)
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _meta(page: str, key: str) -> str | None:
    for pattern in _META_PATTERNS:
        match = re.search(pattern.format(key=re.escape(key)), page, re.I | re.S)
        if match:
            value = html_lib.unescape(match.group("value")).strip()
            if value:
                return value
    return None


def _tag_text(page: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}[^>]*>(?P<value>.*?)</{tag}>", page, re.I | re.S)
    if not match:
        return None
    text = clean_text(match.group("value"), TITLE_LIMIT)
    return text or None


def _title(page: str) -> str:
    for key in _TITLE_KEYS:
        value = _meta(page, key)
        if value:
            return clean_text(value, TITLE_LIMIT)
    return _tag_text(page, "title") or _tag_text(page, "h1") or ""


def _summary(page: str) -> str:
    for key in _SUMMARY_KEYS:
        value = _meta(page, key)
        if value:
            return clean_text(value, SUMMARY_LIMIT)
    match = re.search(
        r"""id\s*=\s*["']js_content["'][^>]*>(?P<value>.*?)</div>""", page, re.I | re.S
    )
    if match:
        return clean_text(match.group("value"), SUMMARY_LIMIT)
    return ""


def _published(page: str) -> datetime | None:
    """按「越具体越优先」的顺序猜发布时间，猜不到返回 None。"""
    for pattern in _EPOCH_PATTERNS:
        match = re.search(pattern, page, re.I)
        if match:
            stamp = int(match.group(1))
            if stamp > 10**11:  # 毫秒级时间戳
                stamp //= 1000
            try:
                return datetime.fromtimestamp(stamp, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
    for key in _DATE_KEYS:
        parsed = parse_datetime(_meta(page, key))
        if parsed:
            return parsed
    for pattern in _ISO_PATTERNS:
        match = re.search(pattern, page, re.I)
        if match:
            parsed = parse_datetime(match.group(1))
            if parsed:
                return parsed
    return None


def _detect_lang(text: str) -> str:
    return "zh" if _CJK.search(text) else "en"


def extract(page: str, url: str) -> dict[str, Any]:
    """把一页 HTML 变成一条消息的字段。公开函数，方便单测直接喂 HTML。"""
    title = _title(page)
    summary = _summary(page)
    return {
        "url": url,
        "title": title or url,
        "summary": summary,
        "published_at": _published(page),
        "site": _meta(page, "og:site_name") or (urlsplit(url).netloc or ""),
        "lang": _detect_lang(f"{title} {summary}"),
    }


def clip_one(url: str, *, fetch: Fetcher | None = None) -> dict[str, Any]:
    fetcher = fetch or _fetch_html
    return extract(fetcher(url), url)


# ── 入库 ────────────────────────────────────────────────────────────────────
def ensure_manual_source(session: Session) -> NewsSource:
    """补录用的固定源，第一次补录时创建；重复调用只会拿到同一条。"""
    source = session.exec(
        select(NewsSource).where(NewsSource.url == MANUAL_SOURCE_URL)
    ).first()
    if source is None:
        source = NewsSource(
            name=MANUAL_SOURCE_NAME,
            url=MANUAL_SOURCE_URL,
            kind="manual",
            lang="zh",
            enabled=True,
        )
        session.add(source)
        session.flush()
    return source


def _dedupe_key(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()


def _fetch_all(pairs: list[tuple[str, str]], fetcher: Fetcher) -> list[dict[str, Any]]:
    """并发抓，但整批有总预算；超预算的链接判失败，不让请求挂太久。"""
    rows: list[dict[str, Any]] = []
    pool = ThreadPoolExecutor(max_workers=min(6, len(pairs)))
    try:
        futures = {
            pool.submit(clip_one, url, fetch=fetcher): (url, key) for url, key in pairs
        }
        done, pending = wait(futures, timeout=TOTAL_BUDGET)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    for future in done:
        url, key = futures[future]
        try:
            rows.append({"url": url, "key": key, "entry": future.result()})
        except ClipError as exc:
            rows.append({"url": url, "key": key, "error": str(exc)[:ERROR_LIMIT]})
        except Exception as exc:  # 解析炸了也只算这一条失败
            rows.append({"url": url, "key": key, "error": f"解析失败：{exc}"[:ERROR_LIMIT]})
    for future in pending:
        url, _key = futures[future]
        rows.append(
            {"url": url, "key": None, "error": f"{int(TOTAL_BUDGET)} 秒内没抓完，已跳过"}
        )

    order = {url: index for index, (url, _key) in enumerate(pairs)}
    rows.sort(key=lambda row: order.get(row["url"], 0))
    return rows


def clip_many(
    session: Session,
    urls: list[str],
    *,
    entity_id: int | None = None,
    fetch: Fetcher | None = None,
) -> dict[str, Any]:
    """批量补录。不 commit —— 由调用方决定事务边界。

    返回 {
        added: 新增条数,
        duplicated: 库里已有（按 dedupe_key 判定）,
        duplicated_urls: [...],
        failed: [{url, error}],
        items: [{title, url, published_at}],
        skipped: 输入里自己重复的条数,
        truncated: 超过每批上限被截掉的条数,
    }
    """
    fetcher = fetch or _fetch_html
    report: dict[str, Any] = {
        "added": 0,
        "duplicated": 0,
        "duplicated_urls": [],
        "failed": [],
        "items": [],
        "skipped": 0,
        "truncated": 0,
    }

    cleaned: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in urls:
        text = (raw or "").strip()
        if not text:
            continue
        if not text.startswith(("http://", "https://")):
            report["failed"].append({"url": text[:80], "error": "不是 http(s) 链接"})
            continue
        if len(text) > URL_LIMIT:
            report["failed"].append(
                {"url": text[:60] + "…", "error": f"链接超过 {URL_LIMIT} 个字符"}
            )
            continue
        key = _dedupe_key(text)
        if key in seen:
            report["skipped"] += 1
            continue
        seen.add(key)
        cleaned.append((text, key))

    if len(cleaned) > MAX_URLS:
        report["truncated"] = len(cleaned) - MAX_URLS
        cleaned = cleaned[:MAX_URLS]
    if not cleaned:
        return report

    # 已在库里的不必再抓一遍：先一次查询判重（dedupe_key 是全表唯一的）
    existing = set(
        session.exec(
            select(NewsItem.dedupe_key).where(
                NewsItem.dedupe_key.in_([key for _url, key in cleaned])
            )
        ).all()
    )
    pending: list[tuple[str, str]] = []
    for url, key in cleaned:
        if key in existing:
            report["duplicated"] += 1
            report["duplicated_urls"].append(url)
        else:
            pending.append((url, key))
    if not pending:
        return report

    source = ensure_manual_source(session)
    now = utcnow()
    for row in _fetch_all(pending, fetcher):
        if row.get("error"):
            report["failed"].append({"url": row["url"], "error": row["error"]})
            continue
        entry = row["entry"]
        session.add(
            NewsItem(
                source_id=source.id,
                entity_id=entity_id,
                guid="",
                url=entry["url"],
                title=entry["title"],
                summary=entry["summary"] or None,
                lang=entry["lang"],
                published_at=entry["published_at"] or now,
                fetched_at=now,
                dedupe_key=row["key"],
            )
        )
        report["added"] += 1
        report["items"].append(
            {
                "title": entry["title"],
                "url": entry["url"],
                "published_at": entry["published_at"],
            }
        )

    source.last_fetch_at = now
    if report["added"]:
        source.last_status = "ok"
        source.last_error = None
    source.item_count = int(
        session.exec(
            select(func.count()).select_from(NewsItem).where(NewsItem.source_id == source.id)
        ).one()
    )
    return report
