"""Agent 推送市场消息接口（POST /api/v1/news）的回归测试。

覆盖：鉴权 scope、字段校验、去重、dry_run、幂等键、公司归属与保留源。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["CIB_DATA_DIR"] = tempfile.mkdtemp(prefix="cib-push-")
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import tokens
from app.db import engine
from app.main import app
from app.models import Entity, NewsItem, NewsSource
from fastapi.testclient import TestClient
from sqlmodel import Session, select

_counter = 0


def _client() -> TestClient:
    global _counter
    if _counter == 1:
        return TestClient(app)
    _counter = 1
    c = TestClient(app)
    c.__enter__()
    return c


def _headers(scopes=("ingest",), name="推送测试"):
    with Session(engine) as s:
        raw, _ = tokens.create_token(s, name=name, scopes=list(scopes))
        s.commit()
    return {"Authorization": f"Bearer {raw}"}


def _entity(name="推送测试公司", name_en="Push Test Co") -> Entity:
    with Session(engine) as s:
        ent = s.exec(select(Entity).where(Entity.name == name)).first()
        if ent is None:
            ent = Entity(kind="competitor", name=name, name_en=name_en)
            s.add(ent)
            s.commit()
            s.refresh(ent)
        return ent


def test_push_creates_item_and_creates_reserved_source():
    c = _client()
    ent = _entity()
    r = c.post(
        "/api/v1/news",
        headers=_headers(),
        json={"items": [{
            "title": "友商发布新款压电泵",
            "url": "https://example.com/push/a?utm_source=agent",
            "summary": "<p>这是摘要</p>",
            "published_at": "2026-11-20T08:00:00Z",
            "entity_name": ent.name,
            "lang": "zh",
        }]},
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["created"] == 1 and data["duplicated"] == 0
    assert data["source"]["name"] == "Agent 推送"
    assert data["items"][0]["url"] == "https://example.com/push/a?utm_source=agent"
    assert data["items"][0]["entity"] == ent.name
    with Session(engine) as s:
        source = s.exec(select(NewsSource).where(NewsSource.url == "agent://push")).first()
        assert source is not None and source.kind == "push"
        item = s.exec(select(NewsItem).where(NewsItem.title == "友商发布新款压电泵")).first()
        assert item is not None
        assert item.summary == "这是摘要"  # HTML 被清洗
        assert item.entity_id == ent.id


def test_push_deduplicates_same_url_across_tracking_params():
    c = _client()
    base = {"title": "去重测试", "published_at": "2026-11-20T09:00:00Z"}
    first = c.post("/api/v1/news", headers=_headers(name="去重1"),
                   json={"items": [{**base, "url": "https://example.com/push/b?utm_medium=x"}]})
    assert first.status_code == 201, first.text
    second = c.post("/api/v1/news", headers=_headers(name="去重2"),
                    json={"items": [{**base, "url": "https://example.com/push/b?utm_source=y"}]})
    assert second.status_code == 200, second.text
    assert second.json()["data"]["duplicated"] == 1
    with Session(engine) as s:
        rows = s.exec(select(NewsItem).where(NewsItem.title == "去重测试")).all()
        assert len(rows) == 1


def test_push_requires_ingest_scope():
    c = _client()
    r = c.post("/api/v1/news", headers=_headers(scopes=("read",), name="只读推送"),
               json={"items": [{"title": "无权限"}]})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_push_rejects_invalid_url_without_partial_write():
    c = _client()
    r = c.post("/api/v1/news", headers=_headers(name="坏链接"),
               json={"items": [
                   {"title": "好的一条", "url": "https://example.com/push/c"},
                   {"title": "坏的一条", "url": "javascript:alert(1)"},
               ]})
    assert r.status_code == 422
    with Session(engine) as s:
        assert s.exec(select(NewsItem).where(NewsItem.title == "好的一条")).first() is None


def test_push_dry_run_does_not_write():
    c = _client()
    r = c.post("/api/v1/news", headers=_headers(name="预演"),
               json={"items": [{"title": "预演消息", "url": "https://example.com/push/d"}],
                     "dry_run": True})
    assert r.status_code == 200 and r.json()["meta"]["dry_run"] is True
    assert r.json()["data"]["created"] == 1
    with Session(engine) as s:
        assert s.exec(select(NewsItem).where(NewsItem.title == "预演消息")).first() is None


def test_push_idempotency_key_replays():
    c = _client()
    body = {"items": [{"title": "幂等推送", "url": "https://example.com/push/e"}]}
    h = {**_headers(name="幂等"), "Idempotency-Key": "push-1"}
    first = c.post("/api/v1/news", headers=h, json=body)
    second = c.post("/api/v1/news", headers=h, json=body)
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["meta"]["replayed"] is True
    with Session(engine) as s:
        assert len(s.exec(select(NewsItem).where(NewsItem.title == "幂等推送")).all()) == 1


def test_push_unknown_company_is_rejected():
    c = _client()
    r = c.post("/api/v1/news", headers=_headers(name="公司校验"),
               json={"items": [{"title": "不存在的公司", "entity_name": "没有这家公司"}]})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_push_without_url_uses_guid_for_dedupe():
    c = _client()
    body = {"items": [{"title": "无链接消息", "guid": "g-1"}]}
    first = c.post("/api/v1/news", headers=_headers(name="无链接1"), json=body)
    second = c.post("/api/v1/news", headers=_headers(name="无链接2"), json=body)
    assert first.status_code == 201
    assert second.json()["data"]["duplicated"] == 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
