"""原文链接解析：Google News 跳转页 → 真实文章地址。

两种跑法都支持：
    pytest tests/test_news_link.py
    python tests/test_news_link.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ["CIB_DATA_DIR"] = tempfile.mkdtemp(prefix="cib-link-")
os.environ["CIB_ADMIN_PASSWORD"] = "link-test-pass-123"
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import news, news_link
from app.db import engine, init_db
from app.main import app
from app.models import NewsItem, NewsSource
from fastapi.testclient import TestClient
from sqlmodel import Session

ADMIN_PW = "link-test-pass-123"
GOOGLE_URL = "https://news.google.com/rss/articles/CBMiU0FVX3lxTE14NW85?oc=5"
REAL_URL = "https://wallstreetcn.com/articles/3775491"

# 文章页只需要 c-wiz 上那两个签名属性；真实页面还有 600 KB 别的噪音，与本解析无关
PAGE = '<html><body><c-wiz data-n-a-sg="SIG-abc" data-n-a-ts="1776982968262006584"></c-wiz></body></html>'
RESPONSE = ")]}'\n" + json.dumps(
    [["wrb.fr", "Fbv4je", json.dumps(["garturlres", REAL_URL, 1]), None, None, None, "generic"]]
)


def _boot():
    init_db()
    client = TestClient(app)
    client.__enter__()
    resp = client.post("/login", data={"username": "admin", "password": ADMIN_PW})
    assert resp.status_code in (200, 303)
    return client


_SOURCE_SEQ = [0]


def _source(session: Session, url: str | None = None) -> NewsSource:
    _SOURCE_SEQ[0] += 1
    source = NewsSource(
        name=f"测试源{_SOURCE_SEQ[0]}",
        url=url or f"https://example.com/feed/{_SOURCE_SEQ[0]}/",
        kind="rss",
        lang="zh",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _item(session: Session, source: NewsSource, *, url: str, title: str, key: str) -> NewsItem:
    item = NewsItem(
        source_id=source.id,
        guid=key,
        url=url,
        title=title,
        summary="摘要",
        lang="zh",
        published_at=datetime(2025, 11, 20, 8, 0, tzinfo=timezone.utc),
        dedupe_key=key,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


# ── 纯解析 ──────────────────────────────────────────────────────────────────
def test_is_google_news():
    assert news_link.is_google_news(GOOGLE_URL)
    assert not news_link.is_google_news("https://wallstreetcn.com/articles/1")
    assert not news_link.is_google_news("https://news.google.com/")
    assert not news_link.is_google_news(None)
    assert not news_link.is_google_news("")


def test_resolve_url_passthrough():
    assert news_link.resolve_url("https://example.com/a") == "https://example.com/a"
    assert news_link.resolve_url("") == ""
    assert news_link.resolve_url(None) == ""


def test_resolve_google_news_parses_signature(monkeypatch):
    calls = []

    def fake_open(url, *, data=None, timeout=15.0, proxy=None):
        calls.append((url, data))
        return PAGE if data is None else RESPONSE

    monkeypatch.setattr(news_link, "_open", fake_open)
    assert news_link.resolve_google_news(GOOGLE_URL) == REAL_URL
    assert len(calls) == 2
    assert calls[0][0] == GOOGLE_URL
    assert "batchexecute" in calls[1][0]
    assert b"garturlreq" in calls[1][1]


def test_resolve_google_news_without_signature_raises(monkeypatch):
    monkeypatch.setattr(news_link, "_open", lambda url, **kw: "<html>nope</html>")
    try:
        news_link.resolve_google_news(GOOGLE_URL)
    except news_link.LinkError as exc:
        assert "签名" in str(exc)
    else:
        raise AssertionError("没有签名参数时应当抛 LinkError")


def test_resolve_google_news_rejects_google_target(monkeypatch):
    bad = ")]}'\n" + json.dumps(
        [["wrb.fr", "Fbv4je", json.dumps(["garturlres", GOOGLE_URL, 1]), None, None, None, "generic"]]
    )
    monkeypatch.setattr(
        news_link, "_open", lambda url, **kw: PAGE if kw.get("data") is None else bad
    )
    try:
        news_link.resolve_google_news(GOOGLE_URL)
    except news_link.LinkError as exc:
        assert "跳转页" in str(exc)
    else:
        raise AssertionError("解析结果仍是跳转页时应当抛 LinkError")


# ── 查询结果里的链接 ────────────────────────────────────────────────────────
def test_query_items_link_prefers_real_url_and_falls_back_to_go():
    init_db()
    with Session(engine) as session:
        source = _source(session)
        cached = _item(session, source, url=GOOGLE_URL, title="已解析 001", key="link-cached-001")
        cached.real_url = REAL_URL
        session.add(cached)
        session.commit()
        pending_item = _item(
            session, source, url=GOOGLE_URL, title="待解析 002", key="link-pending-002"
        )
        plain = _item(
            session, source, url="https://example.com/plain", title="普通 003", key="link-plain-003"
        )

        page = news.query_items(session, keyword="00", limit=10)
        by_id = {row["id"]: row for row in page["items"]}
        assert by_id[cached.id]["link"] == REAL_URL
        assert by_id[pending_item.id]["link"] == f"/news/item/{pending_item.id}/go"
        assert by_id[plain.id]["link"] == "https://example.com/plain"


# ── 跳转路由 ────────────────────────────────────────────────────────────────
def test_go_redirects_to_cached_real_url():
    client = _boot()
    with Session(engine) as session:
        source = _source(session)
        item = _item(session, source, url=GOOGLE_URL, title="缓存命中 101", key="go-cached-101")
        item.real_url = REAL_URL
        session.add(item)
        session.commit()
        item_id = item.id
    resp = client.get(f"/news/item/{item_id}/go", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == REAL_URL
    client.__exit__(None, None, None)


def test_go_resolves_and_caches(monkeypatch):
    client = _boot()
    monkeypatch.setattr(news_link, "resolve_google_news", lambda url, **kw: REAL_URL)
    with Session(engine) as session:
        source = _source(session)
        item = _item(session, source, url=GOOGLE_URL, title="现场解析 102", key="go-resolve-102")
        item_id = item.id
    resp = client.get(f"/news/item/{item_id}/go", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == REAL_URL
    with Session(engine) as session:
        assert session.get(NewsItem, item_id).real_url == REAL_URL
    client.__exit__(None, None, None)


def test_go_falls_back_to_original_on_error(monkeypatch):
    client = _boot()

    def boom(url, **kw):
        raise news_link.LinkError("boom")

    monkeypatch.setattr(news_link, "resolve_google_news", boom)
    with Session(engine) as session:
        source = _source(session)
        item = _item(session, source, url=GOOGLE_URL, title="解析失败 103", key="go-fail-103")
        item_id = item.id
    resp = client.get(f"/news/item/{item_id}/go", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == GOOGLE_URL
    client.__exit__(None, None, None)


def test_go_missing_item_redirects_to_news():
    client = _boot()
    resp = client.get("/news/item/999999/go", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/news")
    client.__exit__(None, None, None)


# ── 后台补解析 ──────────────────────────────────────────────────────────────
def test_backfill_resolves_pending_google_items(monkeypatch):
    monkeypatch.setattr(news_link, "resolve_google_news", lambda url, **kw: REAL_URL)
    with Session(engine) as session:
        source = _source(session)
        first = _item(session, source, url=GOOGLE_URL, title="补解析 201", key="backfill-201")
        second = _item(session, source, url=GOOGLE_URL, title="补解析 202", key="backfill-202")
        plain = _item(
            session, source, url="https://example.com/plain", title="普通 203", key="backfill-203"
        )
        ids = [first.id, second.id, plain.id]
    assert news_link.pending_count() >= 2
    done = news_link.backfill(limit=10, workers=2)
    assert done >= 2
    with Session(engine) as session:
        assert session.get(NewsItem, ids[0]).real_url == REAL_URL
        assert session.get(NewsItem, ids[1]).real_url == REAL_URL
        assert session.get(NewsItem, ids[2]).real_url is None


def test_garturlreq_context_shape_is_the_two_element_form():
    # Google 对这个上下文数组的形状很挑：早先发扁平 20 元素数组，服务端回
    # result=null + [3]（参数非法），永远拿不到 garturlres。这里锁住能用的形状，
    # 防止以后「整理代码」时又把它拍平。
    ctx = news_link._GARTURL_CONTEXT
    assert isinstance(ctx, list) and len(ctx) == 12
    assert isinstance(ctx[0], list) and len(ctx[0]) == 17
    assert ctx[1] == "X" and ctx[2] == "X" and ctx[3] == 1
    assert ctx[4] == [1, 1, 1]


def test_resolve_google_news_passes_proxy_to_both_requests():
    seen = []

    def fake_open(url, **kw):
        seen.append((url, kw.get("proxy")))
        if "batchexecute" in url:
            return RESPONSE
        return PAGE

    with patch.object(news_link, "_open", fake_open):
        assert (
            news_link.resolve_google_news(GOOGLE_URL, proxy="http://127.0.0.1:7890")
            == REAL_URL
        )
    assert seen[0][1] == "http://127.0.0.1:7890"
    assert seen[1][1] == "http://127.0.0.1:7890"


def test_schema_has_real_url_column():
    from sqlalchemy import inspect

    columns = {col["name"] for col in inspect(engine).get_columns("news_items")}
    assert "real_url" in columns


if __name__ == "__main__":
    import inspect as _inspect

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            params = _inspect.signature(fn).parameters
            if not params:
                print("RUN", name)
                fn()
    print("OK")
