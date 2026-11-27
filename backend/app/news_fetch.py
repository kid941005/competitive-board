"""抓取并解析 RSS / Atom / RDF。

为什么不用 feedparser / httpx：

- 生产镜像刻意只装必要依赖。这里只需要「拿字节」和「读标题/链接/摘要/时间」
  四件事，标准库的 urllib + xml.etree 足够，多一个包就多一条升级与漏洞面。
- 解析行为要可控。feedparser 会尽量把畸形源拼出来，而看板上宁可少一条，
  也不要错一条 —— 这里的选择是「解析不出就报错」，让源列表直接显示失败原因，
  而不是让时间线里混进半截数据。

安全：DTD / 实体声明一律拒绝（见 parse_feed）。XML 外部实体（XXE）与实体膨胀
（billion laughs）在标准库里没有开关，在入口按前 4 KB 挡掉最省事 —— 合法的
新闻源不需要 DTD。

测试通过 refresh_source(fetch=...) 注入假抓取，不碰网络。
"""

from __future__ import annotations

import gzip
import hashlib
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from .sanitize import safe_url

USER_AGENT = "CompetitiveBoard/1.1 (+https://github.com/kid941005/competitive-board)"
DEFAULT_TIMEOUT = 10.0
MAX_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 200
TITLE_LIMIT = 300
SUMMARY_LIMIT = 400
PROLOG_LIMIT = 4096
# 只用来认「同一篇稿子」：去掉 utm_* 这类投放参数，否则同一个链接会被当成两条。
TRACKING_KEYS = frozenset({"fbclid", "gclid", "yclid", "msclkid"})


class FeedError(RuntimeError):
    """抓取或解析失败。消息会原样进新闻源的「状态」列，所以要写得让人能照着查。"""


@dataclass(frozen=True)
class FetchResult:
    status: int
    body: bytes = b""
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


@dataclass(frozen=True)
class FeedItem:
    title: str
    url: str | None
    guid: str
    summary: str | None
    published_at: datetime | None


# ── 网络 ────────────────────────────────────────────────────────────────────
def fetch_feed(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BYTES,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    """取一个源。304 原样返回（body 为空），由调用方决定「没更新」怎么记。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
            "text/xml;q=0.8, */*;q=0.5"
        ),
        "Accept-Encoding": "gzip",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # 多读一个字节就能判断「超限」，不必等整包下载完
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FeedError(f"响应超过 {max_bytes // (1024 * 1024)} MB，已跳过")
            return FetchResult(
                status=getattr(response, "status", 200),
                body=_decompress(body, response.headers.get("Content-Encoding")),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResult(304, etag=etag, last_modified=last_modified)
        raise FeedError(f"HTTP {exc.code}") from exc
    except FeedError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise FeedError(f"抓取失败：{exc}") from exc


def _decompress(body: bytes, encoding: str | None) -> bytes:
    name = (encoding or "").lower()
    try:
        if "gzip" in name:
            return gzip.decompress(body)
        if "deflate" in name:
            return zlib.decompress(body)
    except (OSError, zlib.error) as exc:
        raise FeedError("响应解压失败") from exc
    return body


# ── 解析 ────────────────────────────────────────────────────────────────────
def parse_feed(body: bytes, *, base_url: str = "") -> list[FeedItem]:
    """把 RSS 2.0 / Atom / RSS 1.0（RDF）解析成统一结构。

    三种格式的条目元素分别是 item / entry / item，字段名不同但语义一致，
    所以按「本地名」匹配，一套逻辑覆盖三种，不写三份分支。
    """
    if not body.strip():
        raise FeedError("响应为空")
    prolog = body[:PROLOG_LIMIT].lower()
    if b"<!doctype" in prolog or b"<!entity" in prolog:
        raise FeedError("源里带 DTD / 实体声明，为防 XXE 已拒绝解析")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise FeedError(f"不是合法的 XML：{exc}") from exc

    items: list[FeedItem] = []
    for entry in root.iter():
        if _local(entry.tag) not in ("item", "entry"):
            continue
        item = _parse_entry(entry, base_url)
        if item is not None:
            items.append(item)
        if len(items) >= MAX_ITEMS:
            break
    return items


def _parse_entry(entry: ElementTree.Element, base_url: str) -> FeedItem | None:
    fields = _child_map(entry)
    title = clean_text(_text(_first(fields, "title")), TITLE_LIMIT)
    summary = clean_text(
        _text(_first(fields, "description"))
        or _text(_first(fields, "summary"))
        or _text(_first(fields, "encoded"))
        or _text(_first(fields, "content")),
        SUMMARY_LIMIT,
    )
    if not title:
        # 没有标题的条目用摘要开头兜底；两样都没有就丢掉，不值得进时间线
        if not summary:
            return None
        title = summary[:60]

    link = _entry_link(fields)
    if link and base_url and not link.lower().startswith(("http://", "https://")):
        link = urljoin(base_url, link)
    url = safe_url(link) if link else None
    if url and not url.startswith(("http://", "https://")):
        # 站内相对路径对「外部新闻链接」没有意义（会指向本看板），当作没有
        url = None

    published: datetime | None = None
    for key in ("pubdate", "published", "date", "updated", "created"):
        published = parse_datetime(_text(_first(fields, key)))
        if published is not None:
            break

    guid = clean_text(_text(_first(fields, "guid")) or _text(_first(fields, "id")), 200)
    return FeedItem(
        title=title,
        url=url,
        guid=guid or (url or ""),
        summary=summary or None,
        published_at=published,
    )


def _child_map(entry: ElementTree.Element) -> dict[str, list[ElementTree.Element]]:
    """子元素按本地名分组。同名多次出现的（如 Atom 的多个 <link>）要全留着。"""
    out: dict[str, list[ElementTree.Element]] = {}
    for child in entry:
        out.setdefault(_local(child.tag), []).append(child)
    return out


def _first(fields: dict[str, list[ElementTree.Element]], name: str) -> ElementTree.Element | None:
    values = fields.get(name)
    return values[0] if values else None


def _local(tag: str) -> str:
    """去掉 {namespace} 前缀并小写：dc:date、<date>、{http://purl.org/dc/...}date 都归一。"""
    return str(tag).rsplit("}", 1)[-1].lower()


def _text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _entry_link(fields: dict[str, list[ElementTree.Element]]) -> str:
    """Atom 用 <link href>（可能多个 rel），RSS 用 <link> 文本。"""
    for element in fields.get("link", []):
        href = (element.get("href") or "").strip()
        rel = (element.get("rel") or "alternate").strip().lower()
        if href and rel == "alternate":
            return href
    return _text(_first(fields, "link"))


class _TextOnly(HTMLParser):
    """只收正文、丢掉标签。convert_charrefs 顺手把 &amp; 这类实体解回来。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_text(raw: str | None, limit: int) -> str:
    """摘要里常常是整段 HTML，进库前去掉标签并压掉多余空白。"""
    text = (raw or "").strip()
    if not text:
        return ""
    parser = _TextOnly()
    try:
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # 畸形 HTML 不该让整条源失败，退回原文
        pass
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def parse_datetime(raw: str | None) -> datetime | None:
    """RSS 用 RFC 822，Atom 用 ISO 8601；两种都试，取不到就返回 None。"""
    text = (raw or "").strip()
    if not text:
        return None
    value: datetime | None = None
    try:
        value = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        value = None
    if value is None:
        candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            value = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ── 去重键 ──────────────────────────────────────────────────────────────────
def normalize_url(url: str) -> str:
    """认「同一篇稿子」用的规范化链接：忽略协议/域名大小写、末尾斜杠、投放参数、锚点。"""
    parts = urlsplit(url.strip())
    query = "&".join(
        f"{key}={value}"
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    )
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", query, "")
    )


def dedupe_key(url: str | None, source_id: int, guid: str) -> str:
    """有链接就按链接认（跨源合并同一篇稿子），没有链接才退到「源 + guid」。"""
    basis = normalize_url(url) if url else f"src:{source_id}|{guid}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()
