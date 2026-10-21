"""客户采用关系的共用逻辑。

三个入口都要做同一件事 —— 页面表单、`/api/v1/adoptions`、表格导入的
「关联客户」列。各写一份的结果是行为漂移（一处复用同名客户、一处新建），
所以「按名字找客户，找不到才建档」只在这里实现一次。

客户的归一规则与导入建公司一致：忽略空格、标点、大小写。
客户就是 `kind="customer"` 的 Entity，不另建主数据 —— 一家既做友商又当客户时
一个 kind 只能选一个，这是已知取舍（见 models.Adoption 的说明）。
"""

from __future__ import annotations

import re

from sqlmodel import Session, select

from .models import ADOPTION_STATUS, Adoption, Entity

_NAME_NOISE = re.compile(r"[\s\-_.·/\\()（）]+")

# 「关联客户（有公开证据）」这类自由文本里，一条记录用这些符号分隔
_SPLIT = re.compile(r"[；;、\n]+")
# 「华为（WATCH D2 第三方拆解确认）」→ 名字 = 华为，括注 = 证据
_PAREN = re.compile(r"^([^（(]+)[（(](.*)[）)]\s*$")
# 这些是「没有客户证据」的占位写法，不能当成客户名建出实体
_PLACEHOLDERS = {"待补充", "待定", "无", "暂无", "未知", "—", "-", "na", "n/a", "无公开信息"}


def norm_name(name: str) -> str:
    return _NAME_NOISE.sub("", (name or "").strip()).lower()


def find_by_name(session: Session, name: str) -> Entity | None:
    key = norm_name(name)
    if not key:
        return None
    for ent in session.exec(select(Entity)).all():
        if norm_name(ent.name) == key:
            return ent
    return None


def resolve_customer(
    session: Session, customer_id: int | None = None, customer_name: str | None = None
) -> Entity:
    """按 id 或按名字取客户；名字不存在就建一个 kind=customer 的公司。

    同名公司已经存在（哪怕它是友商）就复用，不新建：客户档案重复比少一条采用记录
    难收拾得多。kind 不改 —— 一家既是友商又是客户的情况，这里保持原类型。
    """
    if customer_id:
        ent = session.get(Entity, customer_id)
        if ent is None:
            raise LookupError(f"客户 id={customer_id} 不存在")
        return ent
    name = (customer_name or "").strip()
    if not name:
        raise ValueError("customer_id 与 customer_name 至少给一个")
    ent = find_by_name(session, name)
    if ent is not None:
        return ent
    ent = Entity(kind="customer", name=name)
    session.add(ent)
    session.flush()
    return ent


def split_customer_text(text: str) -> list[tuple[str, str | None]]:
    """把「华为（WATCH D2 拆解确认）；欧姆龙（HeartGuide 2019）」拆成
    [(客户名, 证据或 None), ...]。占位写法（待补充 / 无）直接丢掉。"""
    out: list[tuple[str, str | None]] = []
    for chunk in _SPLIT.split(text or ""):
        piece = chunk.strip().strip("。.")
        if not piece or piece.lower() in _PLACEHOLDERS:
            continue
        m = _PAREN.match(piece)
        if m:
            name, evidence = m.group(1).strip(), m.group(2).strip()
        else:
            name, evidence = piece, None
        if not name or name.lower() in _PLACEHOLDERS:
            continue
        out.append((name, evidence or None))
    return out


def add_from_text(
    session: Session, product, text: str, *, status: str = "rumored"
) -> tuple[list[str], list[str]]:
    """把「关联客户」一段文字落成采用记录。返回 (新增的客户名, 需要人工确认的说明)。

    - 已存在同一 (客户, 型号) 记录时不覆盖、不重复建，记为「已有一条」；
    - 状态默认 rumored（传闻）：公开拆解/媒体口径都还不到「送样」这一步；
    - 客户名拆不出来时不动数据结构，把原文返回给调用方写进备注。
    """
    added: list[str] = []
    skipped: list[str] = []
    for name, evidence in split_customer_text(text):
        try:
            customer = resolve_customer(session, customer_name=name)
        except (ValueError, LookupError):
            skipped.append(f"客户「{name}」无法建档")
            continue
        dup = session.exec(
            select(Adoption).where(
                Adoption.customer_id == customer.id, Adoption.product_id == product.id
            )
        ).first()
        if dup is not None:
            skipped.append(f"{customer.name} 已有采用记录（未覆盖）")
            continue
        session.add(Adoption(
            customer_id=customer.id, product_id=product.id,
            status=status, note=evidence,
        ))
        added.append(customer.name)
    if added:
        session.flush()
    return added, skipped


def status_label(code: str) -> str:
    return ADOPTION_STATUS.get(code, code)
