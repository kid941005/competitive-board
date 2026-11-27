"""手工补录：从网页 HTML 里猜字段、批量入库、去重与权限的回归测试。

两种跑法都支持：
    pytest tests/test_news_clip.py
    python tests/test_news_clip.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ["CIB_DATA_DIR"] = tempfile.mkdtemp(prefix="cib-clip-")
os.environ["CIB_ADMIN_PASSWORD"] = "admin-pass-123"
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, news, news_clip
from app.db import engine, init_db
from app.main import app
from app.models import NewsItem, NewsSource, User
from fastapi.testclient import TestClient
from sqlmodel import Session, select

ADMIN_PW = "admin-pass-123"
VIEWER_PW = "viewer-pass-123"

# 公众号文章页的真实特征：var ct 是秒级时间戳，标题/摘要在 og / description 里
WECHAT = """<!doctype html><html><head>
<meta property="og:title" content="压电泵新品发布：流量提升 30%">
<meta name="description" content="某公司今天发布新一代压电泵，流量与背压同时提升。">
<meta property="og:site_name" content="压电产业观察">
<script>var ct = "1732600000"; var other = 1;</script>
</head><body><div id="js_content"><p>正文内容不应被当作摘要</p></div></body></html>"""

ARTICLE = """<html><head>
<meta property="og:title" content="Frore 公布 AirJet 路线图">
<meta property="article:published_time" content="2025-11-24T10:00:00Z">
</head><body></body></html>"""

NO_TITLE = "<html><body><p>没有标题的页面</p></body></html>"


def _boot():
    init_db()
    client = TestClient(app)
    client.__enter__()
    assert client.post("/login", data={"username": "admin", "password": ADMIN_PW}).status_code in (200, 303)
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/compare").text).group(1)
    return client, csrf


def _page(url: str, html: str):
    def fetch(target, **_kw):
        assert target == url
        return html

    return fetch


def _make_viewer() -> str:
    with Session(engine) as session:
        existing = session.exec(select(User).where(User.username == "staff")).first()
        if existing is None:
            session.add(
                User(username="staff", password_hash=auth.hash_password(VIEWER_PW), role="viewer")
            )
            session.commit()
    return VIEWER_PW


# ── 字段提取 ────────────────────────────────────────────────────────────────
def test_extract_reads_wechat_epoch_and_meta():
    row = news_clip.extract(WECHAT, "https://mp.weixin.qq.com/s/abc")
    assert row["title"] == "压电泵新品发布：流量提升 30%"
    assert "流量与背压同时提升" in row["summary"]
    assert "正文内容" not in row["summary"]  # meta description 优先于 #js_content
    assert row["site"] == "压电产业观察"
    assert row["lang"] == "zh"
    assert row["published_at"] is not None
    assert row["published_at"].tzinfo is not None
    # 1732600000 → 2024-11-26T05:46:40Z（按 UTC 解释，不要再叠加本地时区）
    assert row["published_at"].year == 2024
    assert row["published_at"].hour == 5


def test_extract_reads_article_published_time():
    row = news_clip.extract(ARTICLE, "https://example.com/a")
    assert row["title"] == "Frore 公布 AirJet 路线图"
    assert row["published_at"].isoformat().startswith("2025-11-24T10:00:00")
    # 标题里有中文，按中文归类（lang 只看标题+摘要，不看站点）
    assert row["lang"] == "zh"


def test_extract_falls_back_to_url_when_no_title():
    row = news_clip.extract(NO_TITLE, "https://example.com/untitled")
    assert row["title"] == "https://example.com/untitled"
    assert row["published_at"] is None


# ── 入库 ────────────────────────────────────────────────────────────────────
def test_clip_many_adds_then_skips_duplicates():
    init_db()
    url = "https://example.com/clip-1?utm_source=x"
    with Session(engine) as session:
        report = news_clip.clip_many(session, [url], fetch=_page(url, WECHAT))
        session.commit()
        assert report["added"] == 1
        assert report["duplicated"] == 0
        assert report["items"][0]["title"].startswith("压电泵新品发布")

        # 同一链接（带不同跟踪参数）再补录一次：命中去重键，不重复入库
        again = news_clip.clip_many(session, [url.replace("utm_source=x", "utm_source=y")], fetch=_page(url, WECHAT))
        session.commit()
        assert again["added"] == 0
        assert again["duplicated"] == 1
        assert again["duplicated_urls"]

        total = session.exec(
            select(NewsItem).where(NewsItem.dedupe_key == news_clip._dedupe_key(url))
        ).all()
        assert len(total) == 1


def test_clip_many_single_failure_does_not_block_others():
    init_db()
    ok_url = "https://example.com/clip-ok"
    bad_url = "https://example.com/clip-bad"

    def fetch(target, **_kw):
        if target == bad_url:
            raise news_clip.ClipError("抓取失败：HTTP 404")
        return ARTICLE

    with Session(engine) as session:
        report = news_clip.clip_many(session, [bad_url, ok_url], fetch=fetch)
        session.commit()
    assert report["added"] == 1
    assert len(report["failed"]) == 1
    assert report["failed"][0]["url"] == bad_url
    assert "404" in report["failed"][0]["error"]


def test_clip_many_rejects_non_http_links():
    init_db()

    def fetch(_target, **_kw):  # 不应该被抓
        raise AssertionError("非法链接不该发起抓取")

    with Session(engine) as session:
        report = news_clip.clip_many(session, ["ftp://example.com/a", "just text"], fetch=fetch)
    assert report["added"] == 0
    assert len(report["failed"]) == 2
    assert all("http" in item["error"] for item in report["failed"])


def test_manual_source_is_single_and_skipped_by_auto_refresh():
    init_db()
    url = "https://example.com/clip-manual"
    with Session(engine) as session:
        news_clip.clip_many(session, [url], fetch=_page(url, ARTICLE))
        session.commit()
        sources = session.exec(select(NewsSource).where(NewsSource.kind == "manual")).all()
        assert len(sources) == 1
        assert sources[0].url == news_clip.MANUAL_SOURCE_URL

        def boom(_url, **_kw):  # 自动抓取撞到补录源就会炸
            raise AssertionError("补录源不该参与自动抓取")

        stats = news.refresh_all(session, fetch=boom)
        # 只有补录源时，一轮自动抓取应当是零个源
        assert stats["sources"] == 0


# ── 页面与权限 ──────────────────────────────────────────────────────────────
def test_clip_page_requires_write_permission():
    _boot()
    pw = _make_viewer()
    viewer = TestClient(app)
    viewer.__enter__()
    assert viewer.post("/login", data={"username": "staff", "password": pw}).status_code in (200, 303)
    assert viewer.get("/news/clip", follow_redirects=False).status_code == 403


def test_clip_post_adds_items_and_renders_report():
    client, csrf = _boot()
    html = WECHAT
    urls = "https://example.com/post-a\nhttps://example.com/post-b"
    with patch("app.news_clip._fetch_html", side_effect=lambda url, **_kw: html):
        response = client.post(
            "/news/clip",
            data={"_csrf": csrf, "urls": urls, "entity_id": ""},
        )
    assert response.status_code == 200
    body = response.text
    assert "新增 2 条" in body
    assert "本次结果" in body
    assert "压电泵新品发布" in body


def test_admin_page_marks_manual_source_readonly():
    client, _csrf = _boot()
    with Session(engine) as session:
        news_clip.ensure_manual_source(session)
        session.commit()
    page = client.get("/admin/news")
    assert page.status_code == 200
    assert "手工补录源 · 没有 feed 地址，不参与自动抓取" in page.text


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"PASS {name}")
    print("all clip tests passed")
