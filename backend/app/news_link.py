"""把 Google News 的跳转链接解析成原文地址。

内置的 7 个检索源都来自 Google News RSS，它给的 <link> 是
https://news.google.com/rss/articles/CBMi...?oc=5 这样的跳转页：直接打开是一个
需要 JS 的 200 页面（约 600 KB），用户网络受限时还会打不开 —— 看到的不是原文。

真实地址藏在 Google 的一个 protobuf 请求里：先 GET 文章页，取 <c-wiz> 上的
data-n-a-sg（签名）与 data-n-a-ts（时间戳），再 POST
/_/DotsSplashUi/data/batchexecute（Fbv4je / garturlreq），响应里
"garturlres" 后面就是原文 URL。

为什么不在抓取时同步解析：每篇文章要先下约 600 KB 的中间页，几十条新品一轮就是
几十兆、好几分钟，而抓取本身已经够慢。所以做成「缓存 + 按需/后台补」：

- 点击「原文」时若还没解析过，现解析（约 1~3 秒）再跳转，结果写回 news_items.real_url；
- 后台线程在启动时与每轮抓取后补解析，把存量与新条目逐步填满，让绝大多数点击瞬时完成。

只处理 news.google.com 的链接；别的链接原样返回，一个字节都不碰。
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func
from sqlmodel import Session, select

from . import settings_store
from .db import IS_SQLITE, WRITE_LOCK, engine
from .models import NewsItem

log = logging.getLogger("cib.news_link")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 15.0
PAGE_MAX_BYTES = 3 * 1024 * 1024
ERROR_LIMIT = 300
BACKFILL_BATCH = 40
BACKFILL_WORKERS = 6

# Google 的 garturlreq 需要一份固定形状的上下文参数，原样照搬。
# 形状不能改：这里必须是「两个元素」——第一个是地区/语言上下文数组，第二个 "X"，后面跟
# "X",1,[1,1,1],1,1,null,0,0,null,0。早先按扁平 20 元素写过（zh-CN/CN/…/[1608992183,723341000]），
# 服务端直接回 result=null + [3]（参数非法），拿不到 garturlres。
_GARTURL_CONTEXT = [
    [
        "X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
        None, None, None, None, None, 0, 1,
    ],
    "X",
    "X",
    1,
    [1, 1, 1],
    1,
    1,
    None,
    0,
    0,
    None,
    0,
]


class LinkError(RuntimeError):
    """解析失败：网络错误、页面结构变了、或 Google 返回里没有原文地址。"""


def is_google_news(url: str | None) -> bool:
    """是不是 Google News 的文章跳转链接。别的链接不需要也不该去解析。"""
    if not url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.netloc.endswith("news.google.com") and "/articles/" in parts.path


def _proxy_setting(session: Session) -> str | None:
    """读 news.google_proxy；空串归一成 None，避免 build_opener 收到空代理。"""
    raw = settings_store.get_value(session, "news.google_proxy")
    return (raw or "").strip() or None


def _open(
    url: str,
    *,
    data: bytes | None = None,
    timeout: float = TIMEOUT,
    proxy: str | None = None,
) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            response_ctx = opener.open(request, timeout=timeout)
        else:
            response_ctx = urllib.request.urlopen(request, timeout=timeout)
        with response_ctx as response:
            body = response.read(PAGE_MAX_BYTES)
            if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
                body = gzip.decompress(body)
            return body.decode("utf-8", "ignore")
    except Exception as exc:
        raise LinkError(f"请求失败：{exc}") from exc


def _article_id(url: str) -> str:
    match = re.search(r"/articles/([^?#/]+)", url)
    if not match:
        raise LinkError("链接里没有文章 id")
    return match.group(1)


def _signature(page: str) -> tuple[str, str]:
    sig = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not sig or not ts:
        raise LinkError("页面里没有签名参数，Google 可能改版了")
    return sig.group(1), ts.group(1)


def resolve_google_news(
    url: str, *, timeout: float = TIMEOUT, proxy: str | None = None
) -> str:
    """解析一条 Google News 跳转链接，返回原文地址。"""
    article = _article_id(url)
    page = _open(url, timeout=timeout, proxy=proxy)
    sig, ts = _signature(page)
    request_body = json.dumps(
        ["garturlreq", _GARTURL_CONTEXT, article, ts, sig], separators=(",", ":")
    )
    payload = json.dumps([[["Fbv4je", request_body, None, "generic"]]], separators=(",", ":"))
    body = _open(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        data=urllib.parse.urlencode({"f.req": payload}).encode(),
        timeout=timeout,
        proxy=proxy,
    )
    # 响应里的内层 JSON 是转义过的字符串（\"garturlres\",\"https://...），
    # 所以先定位 garturlres，再取它后面第一个 http(s) 地址，别跟转义符较劲。
    marker = body.find("garturlres")
    match = re.search(r'(https?://[^"\\]+)', body[marker:]) if marker >= 0 else None
    if not match:
        raise LinkError("Google 没有返回原文地址")
    target = match.group(1)
    if is_google_news(target):
        raise LinkError("Google 返回的仍是跳转页")
    return target


def resolve_url(
    url: str | None, *, timeout: float = TIMEOUT, proxy: str | None = None
) -> str:
    """非 Google 链接原样返回；Google 链接解析成原文。"""
    if not url:
        return ""
    if not is_google_news(url):
        return url
    return resolve_google_news(url, timeout=timeout, proxy=proxy)


def resolve_item(
    session: Session,
    item: NewsItem,
    *,
    timeout: float = TIMEOUT,
    proxy: str | None = None,
) -> str:
    """返回这条消息该跳转的地址；解析成功就把原文写回 item.real_url（不 commit）。

    调用方负责事务与写锁：路由里现解析一次，backfill 里批量解析。
    """
    if item.real_url:
        return item.real_url
    if not is_google_news(item.url):
        return item.url or ""
    if proxy is None:
        proxy = _proxy_setting(session)
    target = resolve_google_news(item.url, timeout=timeout, proxy=proxy)
    item.real_url = target
    session.add(item)
    return target


# ── 后台补解析 ──────────────────────────────────────────────────────────────
def pending(session: Session, limit: int) -> list[tuple[int, str]]:
    """取最多 limit 条还没解析的 Google 链接，新的优先（用户最可能点的是最新的）。"""
    rows = session.exec(
        select(NewsItem.id, NewsItem.url)
        .where(NewsItem.real_url.is_(None), NewsItem.url.like("%news.google.com%"))
        .order_by(NewsItem.published_at.desc())
        .limit(limit)
    ).all()
    return [(int(item_id), url) for item_id, url in rows if item_id and url]


def pending_count() -> int:
    with Session(engine) as session:
        return int(
            session.exec(
                select(func.count())
                .select_from(NewsItem)
                .where(NewsItem.real_url.is_(None), NewsItem.url.like("%news.google.com%"))
            ).one()
        )


def backfill(limit: int = BACKFILL_BATCH, *, workers: int = BACKFILL_WORKERS) -> int:
    """解析最多 limit 条待解析消息，返回成功条数。

    网络请求全部在写锁之外并发做（每条约 1~3 秒），只有回写 real_url 那一下才拿写锁，
    和 news.refresh_all 的做法一致 —— 别让网络等待把前台写请求堵住。
    """
    with Session(engine) as session:
        todo = pending(session, limit)
        proxy = _proxy_setting(session)
    if not todo:
        return 0

    def _one(pair: tuple[int, str]) -> tuple[int, str | None]:
        item_id, url = pair
        try:
            return item_id, resolve_google_news(url, proxy=proxy)
        except LinkError:
            return item_id, None

    done = 0
    size = max(1, min(workers, len(todo)))
    with ThreadPoolExecutor(max_workers=size) as pool:
        for item_id, real in pool.map(_one, todo):
            if not real:
                continue
            if IS_SQLITE:
                WRITE_LOCK.acquire()
            try:
                with Session(engine) as session:
                    item = session.get(NewsItem, item_id)
                    if item is None or item.real_url:
                        continue
                    item.real_url = real
                    session.add(item)
                    session.commit()
                    done += 1
            finally:
                if IS_SQLITE:
                    WRITE_LOCK.release()
    return done


_BACKFILL_GUARD = threading.Lock()


def backfill_async() -> bool:
    """后台补解析：启动时与每轮抓取后各叫一次，非阻塞，重复调用直接忽略。

    每轮拿一批，直到没有进展就收工 —— 解析不了的条目（站点结构变了）留在库里，
    下次还会再试，但不会让这个循环空转。
    """
    if not _BACKFILL_GUARD.acquire(blocking=False):
        return False

    def _run() -> None:
        try:
            total = 0
            left = pending_count()
            while left > 0:
                got = backfill()
                total += got
                now_left = pending_count()
                if got == 0 or now_left >= left:
                    break
                left = now_left
            if total:
                log.info(
                    "原文链接解析：补了 %d 条",
                    total,
                    extra={"event": "news_link_backfill"},
                )
        except Exception:
            log.exception("原文链接解析异常", extra={"event": "news_link_backfill_error"})
        finally:
            _BACKFILL_GUARD.release()

    threading.Thread(target=_run, name="news-link", daemon=True).start()
    return True