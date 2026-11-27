"""市场消息：时间线看板与消息源管理。

边界与产品管理一致：看看板只要 data:view，改源要 data:write。

抓取是网络操作，可能几十秒：
- 单个源刷新在请求里同步做 —— 用户点一下就想知道「成了还是 404」；
- 全部刷新丢给后台任务 —— 11 个源串行最坏要一分多钟，不能把浏览器挂在那儿。
  后台任务跑完不回推结果，页面上再点一次刷新即可看到新数据。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlmodel import Session, select

from . import audit, auth, news, news_clip, news_link, settings_store
from .auth import P_VIEW, P_WRITE
from .db import get_session
from .models import AuthSession, Entity, NewsItem, NewsSource, User
from .sanitize import safe_url
from .web_shared import _ctx, _perm, _perm_user, _redirect, _rid, templates

router = APIRouter()

LANGS = {"zh": "中文", "en": "英文"}
NAME_LIMIT = 120
URL_LIMIT = 500


# ── 看板 ────────────────────────────────────────────────────────────────────
@router.get("/news", response_class=HTMLResponse)
def news_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_VIEW)),
    session: Session = Depends(get_session),
    entity: int | None = Query(default=None),
    source: int | None = Query(default=None),
    q: str | None = Query(default=None),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    page = news.query_items(
        session, entity_id=entity, source_id=source, keyword=q
    )
    groups = news.group_by_day(page["items"])
    options = news.filter_options(session)
    return templates.TemplateResponse(
        request=request,
        name="news.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="市场消息",
            page_sub="友商动态与品类新闻 · 按发布时间倒序",
            active="news",
            groups=groups,
            last_day=groups[-1]["day"] if groups else "",
            next_cursor=page["next_cursor"] or "",
            total=page["total"],
            entity_id=entity or "",
            source_id=source or "",
            keyword=(q or "").strip(),
            options=options,
            can_refresh=auth.has_perm(user, P_WRITE),
        ),
    )


@router.get("/news/items")
def news_items(
    pair: tuple[User, AuthSession] = Depends(_perm(P_VIEW)),
    session: Session = Depends(get_session),
    entity: int | None = Query(default=None),
    source: int | None = Query(default=None),
    q: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=news.PAGE_SIZE_DEFAULT),
):
    """无限滚动用：返回「卡片 HTML + 下一个游标」。

    为什么不返回纯 JSON 再让前端拼 DOM：卡片结构只在模板里写一份，
    前端只负责摆放；改样式时不会出现「首屏和后续页长得不一样」。
    """
    page = news.query_items(
        session,
        entity_id=entity,
        source_id=source,
        keyword=q,
        cursor=cursor,
        limit=limit,
    )
    html = templates.env.get_template("_news_cards.html").render(items=page["items"])
    return JSONResponse(
        {"html": html, "next_cursor": page["next_cursor"] or "", "total": page["total"]}
    )


@router.get("/news/item/{item_id}/go")
def news_item_go(
    item_id: int,
    pair: tuple[User, AuthSession] = Depends(_perm(P_VIEW)),
    session: Session = Depends(get_session),
):
    """「原文」跳转：Google News 的跳转页在这里换成真实文章地址。

    没解析过的现解析（约 1~3 秒）再 302，结果写回 real_url 供下次直接用；
    解析失败退回原链接，不让按钮变成死链（至少还能在浏览器里手动点开）。
    """
    item = session.get(NewsItem, item_id)
    if item is None:
        return _redirect("/news", "这条消息已不存在", "err")
    if item.real_url:
        return RedirectResponse(safe_url(item.real_url) or "/news", status_code=302)
    try:
        with news._write_lock():
            target = news_link.resolve_item(session, item)
            if item.real_url:
                session.commit()
    except Exception:
        target = item.url or ""
    return RedirectResponse(safe_url(target) or "/news", status_code=302)


@router.post("/news/refresh")
def news_refresh(
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_WRITE)),
):
    """全部刷新走独立线程：抓完再看，避免请求超时。"""
    if not news.refresh_background():
        return _redirect("/news", "已有一轮抓取在进行，请稍后再看", "err")
    return _redirect("/news", "已开始抓取，稍等片刻后刷新页面查看结果")


# ── 手工补录 ────────────────────────────────────────────────────────────────
def _entity_options(session: Session) -> list[Entity]:
    return session.exec(
        select(Entity)
        .where(Entity.kind.in_(("competitor", "customer", "self")))
        .order_by(Entity.kind, Entity.name)
    ).all()


def _parse_entity(session: Session, raw: str) -> tuple[int | None, str | None]:
    """补录时统一挂一家公司；留空表示不归属。"""
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        target = int(text)
    except ValueError:
        return None, "归属公司不合法"
    if session.get(Entity, target) is None:
        return None, "归属公司不存在"
    return target, None


def _clip_context(session: Session, *, urls_text: str, entity_id: str, report, msg, msg_kind):
    return {
        "entities": _entity_options(session),
        "max_urls": news_clip.MAX_URLS,
        "urls_text": urls_text,
        "entity_id": entity_id,
        "report": report,
        "msg": msg,
        "msg_kind": msg_kind,
    }


@router.get("/news/clip", response_class=HTMLResponse)
def news_clip_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    return templates.TemplateResponse(
        request=request,
        name="news_clip.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="手工补录",
            page_sub="贴网页链接（含公众号文章）→ 抓标题、摘要、发布时间入库",
            active="news",
            **_clip_context(
                session, urls_text="", entity_id="", report=None, msg=None, msg_kind="ok"
            ),
        ),
    )


@router.post("/news/clip", response_class=HTMLResponse)
def news_clip_submit(
    request: Request,
    urls: str = Form(default=""),
    entity_id: str = Form(default=""),
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    _csrf: None = Depends(auth.verify_csrf),
    session: Session = Depends(get_session),
):
    """同步补录：一批最多 12 条，整批抓取有 10 秒总预算（见 news_clip）。

    这里同步而不是丢后台：用户贴完链接就站在页面上等结果，后台跑完不回推，
    反而要来回点。并发抓、总预算封顶，就是为了让这段等待可控。
    """
    user, auth_session = pair
    sset = settings_store.all_values(session)
    target, error = _parse_entity(session, entity_id)
    report = None
    msg: str | None = None
    msg_kind = "ok"
    if error:
        msg, msg_kind = error, "err"
    else:
        lines = [line.strip() for line in (urls or "").splitlines() if line.strip()]
        if not lines:
            msg, msg_kind = "先贴至少一条链接", "err"
        else:
            report = news_clip.clip_many(session, lines, entity_id=target)
            session.commit()
            msg = (
                f"新增 {report['added']} 条 · 重复 {report['duplicated']} 条 · "
                f"失败 {len(report['failed'])} 条"
            )
            msg_kind = "ok" if report["added"] else "err"
    return templates.TemplateResponse(
        request=request,
        name="news_clip.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="手工补录",
            page_sub="贴网页链接（含公众号文章）→ 抓标题、摘要、发布时间入库",
            active="news",
            **_clip_context(
                session,
                urls_text=urls,
                entity_id=entity_id,
                report=report,
                msg=msg,
                msg_kind=msg_kind,
            ),
        ),
    )


# ── 消息源管理 ──────────────────────────────────────────────────────────────
@router.get("/admin/news", response_class=HTMLResponse)
def admin_news_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
    ok: str | None = Query(default=None),
    err: str | None = Query(default=None),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    sources = session.exec(
        select(NewsSource).order_by(NewsSource.enabled.desc(), NewsSource.name)
    ).all()
    # 实时计数而不是读 item_count 字段：手工插库、删过消息的情况下那个字段会漂
    counts = dict(
        session.exec(
            select(NewsItem.source_id, func.count(NewsItem.id)).group_by(NewsItem.source_id)
        ).all()
    )
    entities = session.exec(
        select(Entity)
        .where(Entity.kind.in_(("competitor", "customer", "self")))
        .order_by(Entity.kind, Entity.name)
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="admin_news.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="消息源管理",
            page_sub="RSS / Atom 源，抓取结果与错误都在这里",
            active="admin_news",
            sources=sources,
            counts=counts,
            entities=entities,
            langs=LANGS,
            ok=ok,
            err=err,
        ),
    )


def _source_snapshot(source: NewsSource, entity_name: str | None) -> dict:
    return {
        "name": source.name,
        "url": source.url,
        "lang": source.lang,
        "entity": entity_name or "（不归属）",
        "enabled": "启用" if source.enabled else "停用",
    }


SNAPSHOT_LABELS = {
    "name": "名称",
    "url": "地址",
    "lang": "语言",
    "entity": "归属公司",
    "enabled": "状态",
}


def _parse_form(
    session: Session, *, name: str, url: str, lang: str, entity_id: str, enabled: bool
) -> tuple[dict | None, str | None]:
    """表单校验。返回 (数据, 错误消息)，两者只有一个非空。"""
    clean_name = (name or "").strip()
    if not clean_name:
        return None, "名称不能为空"
    if len(clean_name) > NAME_LIMIT:
        return None, f"名称不能超过 {NAME_LIMIT} 个字符"
    clean_url = safe_url((url or "").strip())
    if not clean_url or not clean_url.startswith(("http://", "https://")):
        return None, "地址必须是 http(s) 链接"
    if len(clean_url) > URL_LIMIT:
        return None, f"地址不能超过 {URL_LIMIT} 个字符"
    clean_lang = (lang or "zh").strip()
    if clean_lang not in LANGS:
        return None, "语言只能是中文或英文"
    target: int | None = None
    if (entity_id or "").strip():
        try:
            target = int(entity_id)
        except ValueError:
            return None, "归属公司不合法"
        if session.get(Entity, target) is None:
            return None, "归属公司不存在"
    return (
        {
            "name": clean_name,
            "url": clean_url,
            "lang": clean_lang,
            "entity_id": target,
            "enabled": enabled,
        },
        None,
    )


def _url_taken(session: Session, url: str, *, skip_id: int | None = None) -> bool:
    stmt = select(NewsSource).where(func.lower(func.trim(NewsSource.url)) == url.lower())
    if skip_id:
        stmt = stmt.where(NewsSource.id != skip_id)
    return session.exec(stmt).first() is not None


@router.post("/admin/news/create")
def create_source(
    name: str = Form(default=""),
    url: str = Form(default=""),
    lang: str = Form(default="zh"),
    entity_id: str = Form(default=""),
    enabled: str | None = Form(default=None),
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    data, error = _parse_form(
        session, name=name, url=url, lang=lang, entity_id=entity_id, enabled=enabled is not None
    )
    if error:
        return _redirect("/admin/news", error, "err")
    if _url_taken(session, data["url"]):
        return _redirect("/admin/news", "这个地址已经在列表里了", "err")

    source = NewsSource(**data)
    session.add(source)
    session.flush()
    audit.record(
        session,
        actor=actor.username,
        action="create",
        object_type="news_source",
        object_id=source.id,
        label=source.name,
        origin="web",
        changes=audit.diff(
            {}, _source_snapshot(source, _entity_name(session, source.entity_id)),
            SNAPSHOT_LABELS,
        ),
        note="新建消息源",
        request_id=_rid(),
    )
    session.commit()
    return _redirect("/admin/news", f"已添加「{source.name}」，点右侧「抓取」试一次")


@router.post("/admin/news/{source_id}/update")
def update_source(
    source_id: int,
    name: str = Form(default=""),
    url: str = Form(default=""),
    lang: str = Form(default="zh"),
    entity_id: str = Form(default=""),
    enabled: str | None = Form(default=None),
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    source = session.get(NewsSource, source_id)
    if source is None:
        return _redirect("/admin/news", "消息源不存在", "err")
    data, error = _parse_form(
        session, name=name, url=url, lang=lang, entity_id=entity_id, enabled=enabled is not None
    )
    if error:
        return _redirect("/admin/news", error, "err")
    if _url_taken(session, data["url"], skip_id=source_id):
        return _redirect("/admin/news", "这个地址已经在列表里了", "err")

    before = _source_snapshot(source, _entity_name(session, source.entity_id))
    for key, value in data.items():
        setattr(source, key, value)
    after = _source_snapshot(source, _entity_name(session, source.entity_id))
    changes = audit.diff(before, after, SNAPSHOT_LABELS)
    if not changes:
        return _redirect("/admin/news", "内容没有变化")
    audit.record(
        session,
        actor=actor.username,
        action="update",
        object_type="news_source",
        object_id=source.id,
        label=source.name,
        origin="web",
        changes=changes,
        request_id=_rid(),
    )
    session.commit()
    return _redirect("/admin/news", f"已保存「{source.name}」")


@router.post("/admin/news/{source_id}/toggle")
def toggle_source(
    source_id: int,
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    source = session.get(NewsSource, source_id)
    if source is None:
        return _redirect("/admin/news", "消息源不存在", "err")
    before = _source_snapshot(source, _entity_name(session, source.entity_id))
    source.enabled = not source.enabled
    after = _source_snapshot(source, _entity_name(session, source.entity_id))
    audit.record(
        session,
        actor=actor.username,
        action="update",
        object_type="news_source",
        object_id=source.id,
        label=source.name,
        origin="web",
        changes=audit.diff(before, after, SNAPSHOT_LABELS),
        note="停用/启用消息源",
        request_id=_rid(),
    )
    session.commit()
    return _redirect(
        "/admin/news",
        f"已{'启用' if source.enabled else '停用'}「{source.name}」",
    )


@router.post("/admin/news/{source_id}/delete")
def delete_source(
    source_id: int,
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    source = session.get(NewsSource, source_id)
    if source is None:
        return _redirect("/admin/news", "消息源不存在", "err")
    name = source.name
    # 消息跟着源一起删：源没了还留着它的消息，时间线上会出现没有出处的条目。
    # 但同一篇稿子若被别的源也收录，那条是独立的行，不受影响。
    removed = session.exec(select(func.count()).select_from(NewsItem).where(
        NewsItem.source_id == source_id
    )).one()
    for item in session.exec(select(NewsItem).where(NewsItem.source_id == source_id)).all():
        session.delete(item)
    audit.record(
        session,
        actor=actor.username,
        action="delete",
        object_type="news_source",
        object_id=source_id,
        label=name,
        origin="web",
        changes=[{"field": "名称", "old": name, "new": ""}],
        note=f"删除消息源，连带清理 {int(removed)} 条消息",
        request_id=_rid(),
    )
    session.delete(source)
    session.commit()
    return _redirect("/admin/news", f"已删除「{name}」及其 {int(removed)} 条消息")


@router.post("/admin/news/{source_id}/refresh")
def refresh_one(
    source_id: int,
    _csrf: None = Depends(auth.verify_csrf),
    actor: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    """单个源同步刷新：用户点一下就要知道结果，所以这里不丢后台。"""
    source = session.get(NewsSource, source_id)
    if source is None:
        return _redirect("/admin/news", "消息源不存在", "err")
    result = news.refresh_source(session, source)
    session.commit()
    if result["status"] == "error":
        return _redirect("/admin/news", f"「{source.name}」抓取失败：{result['error']}", "err")
    if result["status"] == "empty":
        return _redirect("/admin/news", f"「{source.name}」连上了，但源里没有条目", "err")
    return _redirect("/admin/news", f"「{source.name}」新增 {result['added']} 条")


def _entity_name(session: Session, entity_id: int | None) -> str | None:
    if not entity_id:
        return None
    entity = session.get(Entity, entity_id)
    return entity.name if entity else None
