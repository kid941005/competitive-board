"""产品管理与批量导入（Excel / CSV）。"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlmodel import Session, select

from . import audit, auth, config, settings_store
from .auth import P_WRITE, AuthSession, User
from .compare import format_number, metrics_for_categories
from .config import category_label
from .db import get_session
from .importer import CSV_TEMPLATE, import_csv, import_xlsx, xlsx_template
from .models import ADOPTION_STATUS, Adoption, Entity, Product, ProductSpec, SpecMetric, SpecSource
from .numeric import parse_number
from .sanitize import safe_url
from .selection import _load
from .web_shared import (
    _ctx,
    _find_product,
    _perm,
    _perm_user,
    _redirect,
    _rid,
    _validate_status,
    _validate_target,
    templates,
)

SHEET_EXT = {".xlsx", ".xlsm", ".csv", ".txt"}

# 公司名归一：与导入建公司用同一套规则（忽略空格、标点、大小写），
# 否则「村田制作所」和「村田制作所 」会各建一份客户档案。
_NAME_NOISE = re.compile(r"[\s\-_.·/\\()（）]+")

router = APIRouter()

@router.get("/import", response_class=HTMLResponse)
def import_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
    ok: str | None = Query(default=None),
    err: str | None = Query(default=None),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    entities, products, metrics, _ = _load(session)
    ent_by_id = {e.id: e for e in entities}
    return templates.TemplateResponse(
        request=request,
        name="import.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="批量导入",
            page_sub="用 Excel 或 CSV 批量写入参数；型号与友商不存在会自动建档",
            active="import",
            ok=ok,
            err=err,
            entities=entities,
            ent_by_id=ent_by_id,
            product_total=len(products),
            metric_total=len(metrics),
            categories=list(config.CATEGORY_LABEL.items()),
            columns=list(_import_columns()),
        ),
    )


def _import_columns() -> list[tuple[str, str, str]]:
    return [
        ("型号", "必填", "已存在则更新该型号参数；不存在则新建型号"),
        ("友商", "建议填", "公司名称；已存在则复用（忽略空格/标点/大小写）"),
        ("大类", "建议填", "只能填：" + "、".join(config.CATEGORY_LABEL.values())),
        ("指标", "必填", "指标中文名或 key，必须在指标字典中存在，否则该行跳过"),
        ("数值", "必填", "数值型填数字；文本型填原文；留空表示未公开，跳过"),
        ("单位", "建议填",
         "这一列的值，如 mL/min、mbar、Vp。填了就换算到指标字典的单位（mL/min → L/min、"
         "Vp → Vpp ×2、±25V → 50 Vpp）；单位认不出或跨维度（把 kPa 填进流量行）该行报错跳过；"
         "不填视为已是字典单位"),
        ("条件", "可空", "如 @15Vpp，会出现在溯源浮层"),
        ("来源链接", "可空", "越具体越好，便于逐项复核"),
        ("置信度", "可空", "0~1，缺省 1.0"),
    ]


@router.post("/api/import", response_model=None)
async def api_import(
    file: UploadFile,
    _csrf: None = Depends(auth.verify_csrf),
    _user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    """Excel 与 CSV 走同一套校验，只在「读文件」这一步分叉。"""
    sset = settings_store.all_values(session)
    max_bytes = int(sset["datasheet.max_upload_mb"]) * 1024 * 1024
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    if len(raw) > max_bytes:
        raise HTTPException(413, f"文件超过 {sset['datasheet.max_upload_mb']} MB")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SHEET_EXT:
        raise HTTPException(400, f"暂不支持 {ext or '未知'} 格式，请上传 .xlsx / .csv")

    note = f"{file.filename or '表格导入'}"
    if ext in (".xlsx", ".xlsm"):
        report = import_xlsx(session, raw, batch_note=note)
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        report = import_csv(session, text, batch_note=note)
    audit.log_import(session, report, actor=_user.username, origin="import",
                     request_id=_rid())
    return report.as_dict()


@router.get("/api/import/template.xlsx")
def api_import_template_xlsx(_pair=Depends(_perm(P_WRITE))):
    return StreamingResponse(
        io.BytesIO(xlsx_template()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=spec-import-template.xlsx"},
    )


@router.get("/api/import/template")
def api_import_template(_pair=Depends(_perm(P_WRITE))):
    return StreamingResponse(
        io.BytesIO(CSV_TEMPLATE.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=spec-import-template.csv"},
    )


def _spec_metric_map(session: Session) -> dict[int, set[int]]:
    """产品 id → 已填写的指标 id 集合。一次查询覆盖整页，避免逐型号查库。"""
    out: dict[int, set[int]] = {}
    for pid, mid in session.exec(
        select(ProductSpec.product_id, ProductSpec.metric_id)  # type: ignore[misc]
    ).all():
        out.setdefault(int(pid), set()).add(int(mid))
    return out


def _product_completeness(
    product: Product, metrics: list[SpecMetric], filled: dict[int, set[int]]
) -> tuple[int, int]:
    """返回 (已公开项, 该大类适用项)。缺失的指标不计入分子。"""
    pool = metrics_for_categories(metrics, {product.category} if product.category else set())
    ids = {m.id for m in pool if m.id is not None}
    if not ids:
        return 0, 0
    return len(ids & filled.get(product.id or 0, set())), len(pool)


@router.get("/products", response_class=HTMLResponse)
def products_page(
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
    ok: str | None = Query(default=None),
    err: str | None = Query(default=None),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    entities, products, metrics, _ = _load(session)
    ent_by_id = {e.id: e for e in entities}

    filled_map = _spec_metric_map(session)
    table = []
    for p in products:
        filled, total = _product_completeness(p, metrics, filled_map)
        table.append({
            "product": p,
            "entity": ent_by_id.get(p.entity_id),
            "filled": filled,
            "total": total,
            "pct": round(filled / total * 100) if total else 0,
        })
    # 按「大类 → 公司 → 型号」排：型号清单是拿来翻的，同一类的放在一起才好比。
    # 大类顺序取 config 里的声明顺序（压电气泵 / 压电风扇 / 压电液泵），空大类排最后。
    cat_order = {code: i for i, code in enumerate(config.CATEGORY_LABEL)}
    table.sort(key=lambda r: (
        cat_order.get(r["product"].category or "", 99),
        r["product"].category or "~",
        (r["entity"].name if r["entity"] else ""),
        r["product"].model,
    ))
    # 每段第一行带上分组标题（模板据此插一行 <tr class="grouprow">），其余行留空
    prev_cat: str | None = None
    for idx, row in enumerate(table):
        cat = row["product"].category or ""
        row["group"] = (config.category_label(cat) if cat else "未分类") if (idx == 0 or cat != prev_cat) else None
        prev_cat = cat

    # 「新建型号」要能选到所有公司（包括还没型号的 —— 那正是要给它们建第一个型号），
    # 但「按公司筛选」是浏览用，列一家 0 个型号的公司在里面，选了就是个空表格。
    companies = [e for e in entities if e.kind in ("self", "competitor")]
    companies.sort(key=lambda e: (0 if e.kind == "self" else 1, e.name))
    with_products = {r["entity"].id for r in table if r["entity"] is not None}
    filter_companies = [e for e in companies if e.id in with_products]

    return templates.TemplateResponse(
        request=request,
        name="products.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title="产品管理",
            page_sub="直接编辑型号与参数值；这里的改动按人工录入处理，来源记为 manual",
            active="products",
            ok=ok,
            err=err,
            rows=table,
            companies=companies,
            filter_companies=filter_companies,
            categories=list(config.CATEGORY_LABEL.items()),
            cat_all=config.CATEGORY_LABEL,
            product_total=len(products),
        ),
    )


@router.post("/products")
def product_create(
    entity_id: int = Form(...),
    model: str = Form(...),
    category: str = Form(...),
    name: str = Form(default=""),
    status: str = Form(default="active"),
    _csrf: None = Depends(auth.verify_csrf),
    _user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    entity = session.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404, "公司不存在")
    if entity.kind == "customer":
        raise HTTPException(400, "客户不是竞品来源")
    model = _validate_target(category, model)
    status = _validate_status(status)
    if _find_product(session, entity_id, model):
        return _redirect("/products", f"型号 {model} 已存在于该公司，未重复创建", "err")

    product = Product(entity_id=entity_id, model=model, name=name.strip() or model,
                      category=category, status=status)
    session.add(product)
    session.flush()
    audit.record(
        session, actor=_user.username, action="create", object_type="product",
        object_id=product.id, label=product.model, origin="web",
        changes=audit.diff({}, audit.product_snapshot(product), audit.PRODUCT_LABELS),
        request_id=_rid(),
    )
    session.commit()
    return _redirect(f"/products/{product.id}", f"已创建型号 {model}，现在可以录入参数")


@router.get("/products/{product_id}", response_class=HTMLResponse)
def product_edit_page(
    product_id: int,
    request: Request,
    pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
    ok: str | None = Query(default=None),
    err: str | None = Query(default=None),
):
    user, auth_session = pair
    sset = settings_store.all_values(session)
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "型号不存在")

    entities, _, metrics, src_by_id = _load(session)
    ent_by_id = {e.id: e for e in entities}
    pool = metrics_for_categories(metrics, {product.category} if product.category else set())
    specs = {
        s.metric_id: s
        for s in session.exec(
            select(ProductSpec).where(ProductSpec.product_id == product.id)
        ).all()
    }

    rows = []
    for m in pool:
        spec = specs.get(m.id)
        value = ""
        if spec:
            if m.value_type == "number" and spec.value_num is not None:
                value = format_number(spec.value_num, m.precision)
            else:
                value = spec.value_text or ""
        rows.append({
            "metric": m,
            "spec": spec,
            "value": value,
            "source": src_by_id.get(spec.source_id) if spec and spec.source_id else None,
        })

    return templates.TemplateResponse(
        request=request,
        name="product_edit.html",
        context=_ctx(
            user,
            auth_session,
            sset,
            page_title=product.model,
            page_sub=f"{ent_by_id[product.entity_id].name if product.entity_id in ent_by_id else ''}"
                     f" · {category_label(product.category)} · 参数 {len(specs)}/{len(pool)}",
            active="products",
            ok=ok,
            err=err,
            product=product,
            entity=ent_by_id.get(product.entity_id),
            rows=rows,
            categories=list(config.CATEGORY_LABEL.items()),
            statuses=[("active", "在售"), ("eol", "停产"), ("rumored", "传闻")],
            adoptions=_adoptions_of(session, product.id, ent_by_id),
            customers=[e for e in entities if e.kind == "customer"],
            adoption_status=ADOPTION_STATUS,
        ),
    )


def _adoptions_of(session: Session, product_id: int, ent_by_id: dict) -> list[dict]:
    """某型号的客户采用情况：客户名、状态、年份、备注。量产后排在前面。"""
    order = list(ADOPTION_STATUS)
    out = []
    for a in session.exec(select(Adoption).where(Adoption.product_id == product_id)).all():
        cust = ent_by_id.get(a.customer_id)
        out.append({
            "row": a,
            "customer": cust,
            "status_label": ADOPTION_STATUS.get(a.status, a.status),
            "order": order.index(a.status) if a.status in order else 99,
        })
    out.sort(key=lambda r: (-(r["row"].year or 0), r["order"],
                            r["customer"].name if r["customer"] else ""))
    return out


def _norm_company(name: str) -> str:
    return _NAME_NOISE.sub("", (name or "").strip()).lower()


@router.post("/products/{product_id}/adoptions")
def adoption_create(
    product_id: int,
    customer_id: int = Form(default=0),
    customer_name: str = Form(default=""),
    status: str = Form(default="sample"),
    year: str = Form(default=""),
    note: str = Form(default=""),
    _csrf: None = Depends(auth.verify_csrf),
    _pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
):
    """记一条「某客户用了这款泵」。

    客户来源二选一：下拉选已有客户，或直接填名字（不存在就建成 kind=customer 的公司）。
    同名公司已经存在时一律复用，不新建 —— 客户档案重复比少一条采用记录更难清理。
    """
    user, _ = _pair
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "型号不存在")
    if status not in ADOPTION_STATUS:
        return _redirect(f"/products/{product_id}", f"状态「{status}」不可识别", "err")

    year_val: int | None = None
    if (year or "").strip():
        try:
            year_val = int((year or "").strip())
        except ValueError:
            return _redirect(f"/products/{product_id}", f"年份「{year}」不是数字", "err")
        if year_val < 2000 or year_val > 2100:
            return _redirect(f"/products/{product_id}", f"年份 {year_val} 看着不对", "err")

    customer: Entity | None = None
    if customer_id:
        customer = session.get(Entity, customer_id)
        if customer is None:
            return _redirect(f"/products/{product_id}", f"客户 id={customer_id} 不存在", "err")
    else:
        name = (customer_name or "").strip()
        if not name:
            return _redirect(f"/products/{product_id}", "请选择客户或填写客户名称", "err")
        key = _norm_company(name)
        for e in session.exec(select(Entity)).all():
            if _norm_company(e.name) == key:
                customer = e
                break
        if customer is None:
            customer = Entity(kind="customer", name=name)
            session.add(customer)
            session.flush()
            audit.record(
                session, actor=user.username, action="create", object_type="entity",
                object_id=customer.id, label=name, origin="web",
                changes=[{"field": "类型", "old": "", "new": "客户"}],
                note="记客户采用情况时顺手建档", request_id=_rid(),
            )

    exists = session.exec(
        select(Adoption).where(
            Adoption.customer_id == customer.id, Adoption.product_id == product.id
        )
    ).first()
    if exists is not None:
        return _redirect(
            f"/products/{product_id}",
            f"{customer.name} 对 {product.model} 已有记录，改那一条的状态即可", "err",
        )

    row = Adoption(
        customer_id=customer.id, product_id=product.id, status=status,
        year=year_val, note=(note or "").strip() or None,
    )
    session.add(row)
    session.flush()
    audit.record(
        session, actor=user.username, action="create", object_type="adoption",
        object_id=row.id, label=f"{customer.name} · {product.model}", origin="web",
        changes=[
            {"field": "客户", "old": "", "new": customer.name},
            {"field": "型号", "old": "", "new": product.model},
            {"field": "状态", "old": "", "new": ADOPTION_STATUS[status]},
        ],
        note=note or None, request_id=_rid(),
    )
    session.commit()
    return _redirect(f"/products/{product_id}", f"已记录：{customer.name} 采用 {product.model}")


@router.post("/products/{product_id}/adoptions/{adoption_id}/delete")
def adoption_delete(
    product_id: int,
    adoption_id: int,
    _csrf: None = Depends(auth.verify_csrf),
    _pair: tuple[User, AuthSession] = Depends(_perm(P_WRITE)),
    session: Session = Depends(get_session),
):
    user, _ = _pair
    row = session.get(Adoption, adoption_id)
    if row is None or row.product_id != product_id:
        return _redirect(f"/products/{product_id}", "这条采用记录不存在", "err")
    product = session.get(Product, product_id)
    customer = session.get(Entity, row.customer_id)
    label = f"{customer.name if customer else row.customer_id} · " \
            f"{product.model if product else product_id}"
    session.delete(row)
    audit.record(
        session, actor=user.username, action="delete", object_type="adoption",
        object_id=adoption_id, label=label, origin="web",
        changes=[{"field": "客户采用", "old": "存在", "new": "已删除"}],
        request_id=_rid(),
    )
    session.commit()
    return _redirect(f"/products/{product_id}", "已删除该采用记录")


@router.post("/products/{product_id}")
def product_save_meta(
    product_id: int,
    model: str = Form(...),
    name: str = Form(default=""),
    category: str = Form(...),
    status: str = Form(default="active"),
    datasheet_url: str = Form(default=""),
    note: str = Form(default=""),
    _csrf: None = Depends(auth.verify_csrf),
    _user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "型号不存在")
    model = _validate_target(category, model)
    status = _validate_status(status)

    other = _find_product(session, product.entity_id, model)
    if other and other.id != product.id:
        owner = session.get(Entity, product.entity_id)
        return _redirect(
            f"/products/{product_id}",
            f"型号 {model} 已被{owner.name if owner else '同一公司'}另一条记录占用",
            "err",
        )

    if product.category and category != product.category:
        # 换大类会让历史参数落到错误的指标体系里，必须先清空或另建型号
        has_specs = session.exec(
            select(ProductSpec).where(ProductSpec.product_id == product.id)
        ).first()
        if has_specs:
            return _redirect(
                f"/products/{product_id}",
                f"该型号已有参数，不能从「{category_label(product.category)}」改成"
                f"「{category_label(category)}」；请先清空参数或另建型号",
                "err",
            )

    before = audit.product_snapshot(product)
    product.model = model
    product.name = name.strip() or model
    product.category = category
    product.status = status
    product.datasheet_url = safe_url(datasheet_url)
    product.note = note.strip() or None
    session.add(product)
    audit.record(
        session, actor=_user.username, action="update", object_type="product",
        object_id=product.id, label=product.model, origin="web",
        changes=audit.diff(before, audit.product_snapshot(product), audit.PRODUCT_LABELS),
        request_id=_rid(),
    )
    session.commit()
    return _redirect(f"/products/{product_id}", "型号信息已保存")


@router.post("/products/{product_id}/specs")
async def product_save_specs(
    product_id: int,
    request: Request,
    _csrf: None = Depends(auth.verify_csrf),
    user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "型号不存在")

    form = await request.form()
    _, _, metrics, _ = _load(session)
    pool = metrics_for_categories(metrics, {product.category} if product.category else set())
    existing = {
        s.metric_id: s
        for s in session.exec(
            select(ProductSpec).where(ProductSpec.product_id == product.id)
        ).all()
    }

    before_specs = audit.spec_snapshot(session, product.id, {m.id: m for m in metrics})

    # 懒建出处：只是打开页面随手保存一次、什么都没改时，不该多出一条 manual 来源
    _source_holder: list[SpecSource] = []

    def _source() -> SpecSource:
        if not _source_holder:
            src = SpecSource(
                kind="manual",
                title=f"{product.model} 手工编辑",
                captured_at=datetime.now(timezone.utc),
                note=f"录入人 {user.username}；人工录入视为已核对，可逐项改回未复核",
            )
            session.add(src)
            session.flush()
            _source_holder.append(src)
        return _source_holder[0]

    written = updated = removed = unchanged = 0
    errors: list[str] = []
    for m in pool:
        if m.id is None:
            continue
        # 表单里根本没出现这个指标 = 本次没打算动它。
        # 早期版本把「没提交」当成「清空」，只要页面漏渲染一项（或指标字典新增了一项）
        # 就会把已有参数静默删掉 —— 必须区分「空值」与「未提交」。
        field = f"{m.id}:v"
        if field not in form:
            continue
        raw = str(form.get(field, "")).strip()
        cond = str(form.get(f"{m.id}:c", "")).strip() or None
        verified = form.get(f"{m.id}:ok") == "on"
        cur = existing.get(m.id)

        if not raw:
            # 空值 = 未公开。已存在的行删掉，不留「有行无值」的脏数据
            if cur:
                session.delete(cur)
                removed += 1
            continue

        if m.value_type == "number":
            num = parse_number(raw)
            if num is None:
                errors.append(f"{m.name}：要求数值，收到「{raw}」，未保存")
                continue
            value_text = None
        else:
            num, value_text = None, raw

        if cur:
            # 「值/条件没变」与「改了这一格」必须分开处理：页面把每个指标都渲染成输入框，
            # 所以保存一次会把整页的值都回帖上来。原来无条件改写 source_id 与 confidence，
            # 结果是「只改一格 → 其余 20 项的出处全变成 manual、置信度全变 100%」，
            # 溯源链接从此指向一条空来源，且日志只记了那一格，损失不可追。
            value_changed = (
                cur.value_num != num
                or (cur.value_text or None) != (value_text or None)
                or (cur.condition or None) != cond
            )
            if not value_changed and cur.verified == verified:
                unchanged += 1
                continue

            cur.value_num, cur.value_text = num, value_text
            cur.condition = cond
            cur.verified = verified
            if value_changed:
                # 只有真改了值，才轮到「人工录入」这条来源接管，置信度才提到 1.0
                cur.source_id = _source().id
                cur.confidence = 1.0
            cur.updated_at = datetime.now(timezone.utc)
            session.add(cur)
            updated += 1
        else:
            session.add(ProductSpec(
                product_id=product.id, metric_id=m.id,
                value_num=num, value_text=value_text, condition=cond,
                source_id=_source().id, confidence=1.0, verified=verified,
            ))
            written += 1

    audit.record(
        session, actor=user.username, action="update", object_type="spec",
        object_id=product.id, label=product.model, origin="web",
        changes=audit.diff(before_specs, audit.spec_snapshot(session, product.id, {m.id: m for m in metrics})),
        note=f"新增 {written}、更新 {updated}、清除 {removed}、未变 {unchanged}",
        request_id=_rid(),
    )
    session.commit()
    msg = f"已保存：新增 {written}、更新 {updated}、清除 {removed}"
    if unchanged:
        msg += f"、未变 {unchanged}"
    if errors:
        msg += "；" + "；".join(errors)
        return _redirect(f"/products/{product_id}", msg, "err")
    return _redirect(f"/products/{product_id}", msg)


@router.post("/products/{product_id}/delete")
def product_delete(
    product_id: int,
    _csrf: None = Depends(auth.verify_csrf),
    _user: User = Depends(_perm_user(P_WRITE)),
    session: Session = Depends(get_session),
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "型号不存在")
    model = product.model
    # 删除是唯一「不记就永远查不到」的操作：先把即将消失的数据写成快照
    snapshot = audit.spec_snapshot(session, product.id)
    changes = [
        {"field": f"型号 · {audit.PRODUCT_LABELS.get(k, k)}", "old": audit._s(v), "new": None}
        for k, v in audit.product_snapshot(product).items() if v not in (None, "")
    ]
    changes += [{"field": k, "old": v, "new": None} for k, v in snapshot.items()]
    audit.record(
        session, actor=_user.username, action="delete", object_type="product",
        object_id=product.id, label=model, origin="web", changes=changes,
        note=f"删除型号及其 {len(snapshot)} 项参数", request_id=_rid(),
    )

    for spec in session.exec(
        select(ProductSpec).where(ProductSpec.product_id == product.id)
    ).all():
        session.delete(spec)
    # 先 flush：ProductSpec 与 Product 之间没有 ORM relationship，
    # 同一批提交时 SQLAlchemy 不保证先删子表，SQLite 外键会直接报错
    session.flush()
    session.delete(product)
    session.commit()
    return _redirect("/products", f"已删除型号 {model}（含其全部参数）")


# ── 管理员：账号管理 ───────────────────────────────────────────────────────
