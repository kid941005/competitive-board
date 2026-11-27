"""按友商公司名生成检索源的回归测试。"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

os.environ["CIB_DATA_DIR"] = tempfile.mkdtemp(prefix="cib-company-src-")
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _http import enable_csrf
from app.db import engine, init_db
from app.main import app
from app.models import Entity, NewsSource
from fastapi.testclient import TestClient
from sqlmodel import Session, select

_client_obj: TestClient | None = None


def _boot():
    global _client_obj
    if _client_obj is not None:
        return _client_obj
    init_db()
    c = TestClient(app)
    c.__enter__()
    r = c.post("/login", data={"username": "admin", "password": "AdminPass123"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    enable_csrf(c)
    _client_obj = c
    return c


def _csrf(c: TestClient) -> str:
    html = c.get("/admin/news").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "消息源管理页没有 CSRF 令牌"
    return m.group(1)


def test_company_keyword_page_and_generation():
    c = _boot()
    with Session(engine) as s:
        if s.exec(select(Entity).where(Entity.name == "友商甲")).first() is None:
            s.add(Entity(kind="competitor", name="友商甲", name_en="Alpha"))
        if s.exec(select(Entity).where(Entity.name == "客户乙")).first() is None:
            s.add(Entity(kind="customer", name="客户乙"))
        s.commit()

    page = c.get("/admin/news").text
    assert "按友商公司名生成检索源" in page
    assert "当前友商" in page

    before = {s.name for s in _sources()}
    r = c.post("/admin/news/company-keywords", data={"_csrf": _csrf(c), "lang": "both"},
               follow_redirects=True)
    assert r.status_code == 200
    assert "已生成 2 个友商检索源" in r.text
    names = {s.name for s in _sources()}
    assert "友商甲 · 中文检索" in names
    assert "Alpha · 英文检索" in names
    assert "客户乙 · 中文检索" not in names

    second = c.post("/admin/news/company-keywords", data={"_csrf": _csrf(c), "lang": "both"},
                    follow_redirects=True)
    assert second.status_code == 200
    assert "没有新建" in second.text
    assert {s.name for s in _sources()} == names | before


def _sources():
    with Session(engine) as s:
        return s.exec(select(NewsSource)).all()


def test_company_source_spec_uses_name_as_keyword():
    from app import news_sources

    spec = news_sources.company_source("友商甲", "Alpha", lang="en")
    assert spec is not None
    assert spec["name"] == "Alpha · 英文检索"
    assert "q=%22Alpha%22" in spec["url"] and "hl=en-US" in spec["url"]
    zh = news_sources.company_source("友商甲", None, lang="zh")
    assert zh is not None and zh["name"] == "友商甲 · 中文检索"
    assert "hl=zh-CN" in zh["url"] and "news.google.com" in zh["url"]
    assert news_sources.company_source("", None, lang="zh") is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
