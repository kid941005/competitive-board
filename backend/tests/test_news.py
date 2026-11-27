"""市场消息：抓取解析、去重、游标翻页、页面渲染与源管理的回归测试。

两种跑法都支持：
    pytest tests/test_news.py
    python tests/test_news.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ["CIB_DATA_DIR"] = tempfile.mkdtemp(prefix="cib-news-")
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import news, settings_store
from app.db import engine, init_db
from app.main import app
from app.models import Entity, NewsItem, NewsSource
from app.news_fetch import FeedError, FetchResult, dedupe_key, fetch_feed, parse_feed
from fastapi.testclient import TestClient
from sqlmodel import Session, select

ADMIN_PW = "AdminPass123"

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>村田发布新款压电泵</title>
  <link>https://example.com/a?utm_source=rss</link>
  <description>&lt;p&gt;新品摘要&lt;/p&gt;</description>
  <pubDate>Tue, 25 Nov 2025 08:00:00 GMT</pubDate><guid>a-1</guid></item>
<item><title>Second</title><link>https://example.com/b</link>
  <description>b</description>
  <pubDate>Wed, 26 Nov 2025 09:30:00 +0000</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>f</title>
<entry><title>Atom 条目</title><link rel="alternate" href="/news/1"/>
  <summary>摘要</summary><published>2025-11-24T10:00:00Z</published><id>tag:1</id></entry>
</feed>"""


def _boot():
    init_db()
    client = TestClient(app)
    client.__enter__()
    assert client.post("/login", data={"username": "admin", "password": ADMIN_PW}).status_code in (200, 303)
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/compare").text).group(1)
    return client, csrf


def _add_source(session: Session, name: str = "测试源", url: str = "https://example.com/feed/", **kw):
    source = NewsSource(name=name, url=url, kind="rss", lang="zh", **kw)
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _seed_items(session: Session, source: NewsSource, count: int, *, entity_id=None):
    base = datetime(2025, 11, 1, 8, 0, tzinfo=timezone.utc)
    for i in range(count):
        session.add(
            NewsItem(
                source_id=source.id,
                entity_id=entity_id,
                guid=f"g{i}",
                url=f"https://example.com/n{i}",
                title=f"标题 {i}",
                summary="摘要",
                lang="zh",
                published_at=base + timedelta(hours=i),
                fetched_at=base,
                # dedupe_key 全表唯一：同一份假数据喂给多个源时要带上源 id
                dedupe_key=f"src{source.id}-{i}",
            )
        )
    session.commit()


def _fetcher(body: str, *, etag: str | None = "v1"):
    def fetch(_url, **_kw):
        return FetchResult(status=200, body=body.encode("utf-8"), etag=etag)

    return fetch


# ── 解析 ────────────────────────────────────────────────────────────────────
def test_parse_rss_extracts_title_link_summary_and_time():
    items = parse_feed(RSS.encode("utf-8"))
    assert len(items) == 2
    first = items[0]
    assert first.title == "村田发布新款压电泵"
    assert first.url == "https://example.com/a?utm_source=rss"
    # 摘要里的 HTML 标签必须被去掉，否则模板会把它当文本原样吐出来
    assert first.summary == "新品摘要"
    assert first.published_at == datetime(2025, 11, 25, 8, 0, tzinfo=timezone.utc)
    assert items[1].published_at == datetime(2025, 11, 26, 9, 30, tzinfo=timezone.utc)


def test_parse_atom_resolves_relative_link_and_iso_time():
    items = parse_feed(ATOM.encode("utf-8"), base_url="https://xmems.com/feed/")
    assert len(items) == 1
    assert items[0].url == "https://xmems.com/news/1"
    assert items[0].published_at == datetime(2025, 11, 24, 10, 0, tzinfo=timezone.utc)


def test_doctype_is_rejected_before_parsing():
    # 带 DTD 的文档可以声明外部实体，解析前直接拒掉（XXE）
    bad = b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "y">]><rss></rss>'
    try:
        parse_feed(bad)
    except FeedError:
        pass
    else:
        raise AssertionError("DTD 必须被拒绝")


def test_dedupe_key_ignores_tracking_params_and_anchor():
    a = dedupe_key("https://Example.com/x/?utm_source=rss#top", 1, "")
    b = dedupe_key("https://example.com/x", 2, "")
    assert a == b
    # 没有链接时退回源 + guid：同一源里 guid 不同就是两条
    assert dedupe_key(None, 7, "abc") != dedupe_key(None, 7, "abd")


# ── 入库 ────────────────────────────────────────────────────────────────────
def test_refresh_stores_items_once():
    init_db()
    with Session(engine) as session:
        source = _add_source(session)
        first = news.refresh_source(session, source, fetch=_fetcher(RSS))
        session.commit()
        assert first["added"] == 2
        assert first["status"] == "ok"
        # 同一份 feed 再抓一遍：去重键挡住重复入库
        second = news.refresh_source(session, source, fetch=_fetcher(RSS))
        session.commit()
        assert second["added"] == 0
        assert source.item_count == 2
        assert len(session.exec(select(NewsItem)).all()) == 2


def test_not_modified_marks_ok_without_adding():
    init_db()
    with Session(engine) as session:
        source = _add_source(session, name="304 源", url="https://example.com/304/")
        source.last_error = "上次的错"
        session.add(source)
        session.commit()
        result = news.refresh_source(
            session, source, fetch=lambda _u, **_kw: FetchResult(status=304)
        )
        session.commit()
        assert result == {"source": "304 源", "status": "ok", "added": 0, "error": None}
        assert source.last_error is None


def test_fetch_error_is_recorded_on_source():
    init_db()
    with Session(engine) as session:
        source = _add_source(session, name="坏源", url="https://example.com/bad/")

        def boom(_url, **_kw):
            raise FeedError("HTTP 500")

        result = news.refresh_source(session, source, fetch=boom)
        session.commit()
        assert result["status"] == "error"
        assert "HTTP 500" in result["error"]
        assert source.last_status == "error"
        assert "HTTP 500" in source.last_error


# ── 查询与页面 ──────────────────────────────────────────────────────────────
def test_cursor_paging_has_no_duplicates_and_keeps_desc_order():
    client, _csrf = _boot()
    with Session(engine) as session:
        source = _add_source(session, name="翻页源", url="https://example.com/page/")
        _seed_items(session, source, 5)
        source_id = source.id

    titles: list[str] = []
    cursor = None
    for _ in range(5):
        # 同一进程里的用例共用一个库，翻页断言必须按源收窄
        url = f"/news/items?limit=2&source={source_id}" + (f"&cursor={cursor}" if cursor else "")
        payload = client.get(url).json()
        assert payload["total"] == 5
        titles.extend(re.findall(r"标题 \d", payload["html"]))
        cursor = payload["next_cursor"]
        if not cursor:
            break
    # 游标翻页不能重复也不能漏：5 条消息正好被三页取完，顺序按发布时间倒序
    assert titles == ["标题 4", "标题 3", "标题 2", "标题 1", "标题 0"], titles
    client.__exit__(None, None, None)


def test_news_page_renders_timeline_and_requires_login():
    client, _csrf = _boot()
    with Session(engine) as session:
        source = _add_source(session, name="页面源", url="https://example.com/view/")
        _seed_items(session, source, 3)
    page = client.get("/news")
    assert page.status_code == 200
    assert "标题 2" in page.text and "newsWall" in page.text
    client.__exit__(None, None, None)

    anon = TestClient(app)
    anon.__enter__()
    # 未登录：页面跳登录页（TestClient 默认跟随跳转，这里要看不跟随的结果）
    assert anon.get("/news", follow_redirects=False).status_code == 303
    assert anon.get("/news/items").status_code == 401
    anon.__exit__(None, None, None)


def test_entity_filter_and_options():
    client, _csrf = _boot()
    with Session(engine) as session:
        entity = Entity(kind="competitor", name="筛选测试公司", region="中国", tier="B")
        session.add(entity)
        session.commit()
        session.refresh(entity)
        hit = _add_source(session, name="命中源", url="https://example.com/hit/")
        miss = _add_source(session, name="未命中源", url="https://example.com/miss/")
        _seed_items(session, hit, 2, entity_id=entity.id)
        _seed_items(session, miss, 1)

        options = news.filter_options(session)
        names = [row["name"] for row in options["sources"]]
        assert "命中源" in names and "未命中源" in names

        payload = news.query_items(session, entity_id=entity.id)
        assert payload["total"] == 2
        assert all(item["entity_name"] == "筛选测试公司" for item in payload["items"])
    client.__exit__(None, None, None)


# ── 源管理 ──────────────────────────────────────────────────────────────────
def test_admin_source_crud_and_delete_cascades():
    client, csrf = _boot()
    client.headers["X-CSRF-Token"] = csrf

    created = client.post(
        "/admin/news/create",
        data={"name": "新源", "url": "https://example.com/new/feed/", "lang": "en", "enabled": "1"},
    )
    assert created.status_code in (200, 303)

    with Session(engine) as session:
        source = session.exec(select(NewsSource).where(NewsSource.name == "新源")).one()
        _seed_items(session, source, 2)
        source_id = source.id

    # 同一地址不能登记两次
    dup = client.post(
        "/admin/news/create",
        data={"name": "重复源", "url": "https://example.com/new/feed/", "lang": "en"},
    )
    assert dup.status_code in (200, 303)
    with Session(engine) as session:
        assert len(session.exec(select(NewsSource).where(NewsSource.name == "重复源")).all()) == 0

    client.post(f"/admin/news/{source_id}/toggle")
    with Session(engine) as session:
        assert session.get(NewsSource, source_id).enabled is False

    # 删除源要连带删掉它的消息，否则时间线会留下指向空源的孤儿行
    client.post(f"/admin/news/{source_id}/delete")
    with Session(engine) as session:
        assert session.get(NewsSource, source_id) is None
        assert session.exec(select(NewsItem).where(NewsItem.source_id == source_id)).all() == []
    client.__exit__(None, None, None)


def test_refresh_minutes_setting_is_registered():
    spec = settings_store.SPEC_INDEX["news.refresh_minutes"]
    assert spec.kind == "int"
    assert spec.minimum == 0 and spec.maximum == 1440
    assert spec.group == "市场消息"


def test_default_lang_setting_is_registered():
    spec = settings_store.SPEC_INDEX["news.default_lang"]
    assert spec.kind == "str"
    assert spec.default == "zh"
    assert spec.choices == ("zh", "en")


def test_google_proxy_setting_validates_http_url():
    spec = settings_store.SPEC_INDEX["news.google_proxy"]
    assert spec.default == ""
    assert spec.parse("") == ("", None)
    assert spec.parse("http://127.0.0.1:7890") == ("http://127.0.0.1:7890", None)
    # 没有 scheme、非 http(s)、带路径都不能存，避免 urllib 里报奇怪的错
    assert spec.parse("127.0.0.1:7890")[1] is not None
    assert spec.parse("ftp://127.0.0.1:7890")[1] is not None
    assert spec.parse("http://127.0.0.1:7890/path")[1] is not None


def test_admin_page_marks_default_lang_selected_and_has_proxy_setting():
    client, _ = _boot()
    try:
        assert 'value="zh" selected' in client.get("/admin/news").text
        assert 'name="news.google_proxy"' in client.get("/admin/settings").text
    finally:
        client.__exit__(None, None, None)


def test_refresh_uses_proxy_only_for_google_sources():
    with Session(engine) as session:
        google = _add_source(
            session,
            name="谷歌代理源",
            url="https://news.google.com/rss/search?q=proxy-test",
        )
        normal = _add_source(
            session,
            name="普通代理源",
            url="https://example.com/proxy-normal/feed/",
        )
        ok, err = settings_store.update(session, "news.google_proxy", "http://127.0.0.1:7890", "test")
        assert ok, err
        session.commit()
        calls = []

        def fetch(url, **kw):
            calls.append((url, kw.get("proxy")))
            return FetchResult(status=304, body=b"", etag=None)

        try:
            news.refresh_source(session, google, fetch=fetch)
            news.refresh_source(session, normal, fetch=fetch)
        finally:
            settings_store.update(session, "news.google_proxy", "", "test")
            session.commit()
    assert calls == [
        ("https://news.google.com/rss/search?q=proxy-test", "http://127.0.0.1:7890"),
        ("https://example.com/proxy-normal/feed/", None),
    ]


def test_fetch_feed_builds_proxy_opener_when_proxy_given():
    seen = {}

    class FakeResponse:
        status = 200
        headers = {}

        def read(self, _n):
            return b'<rss version="2.0"><channel></channel></rss>'

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class FakeOpener:
        def open(self, _request, timeout):
            return FakeResponse()

    def fake_build(handler):
        seen["handler"] = handler
        return FakeOpener()

    with patch("app.news_fetch.urllib.request.build_opener", fake_build):
        result = fetch_feed(
            "https://news.google.com/rss/search?q=x", proxy="http://127.0.0.1:7890"
        )
    assert result.status == 200
    assert seen["handler"].proxies == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
