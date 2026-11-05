"""公司 / 型号的查找规则 —— 页面、接口与导入共用同一份实现。

为什么单独成模块：这套「名称怎么算同一家」「型号怎么算同一款」的规则原先散在
三处（web_shared、api_v1、adoptions），每处各写一遍循环。归一规则一旦分叉，
就会出现「页面上复用了、接口又新建一份」这种最难收拾的重复档案。

规则本身只有两条：
- 公司按 `entity_names.key` 归一（忽略空白、标点、大小写与括注）；
- 型号按「同公司 + strip().casefold()」比较，与数据库唯一索引
  `ux_products_entity_model` 的口径完全一致 —— 代码里判为不同、索引判为相同，
  就会在提交时才炸 IntegrityError。
"""

from __future__ import annotations

from sqlmodel import Session, select

from . import entity_names
from .models import Entity, Product


def find_entity(session: Session, name: str) -> tuple[Entity | None, list[Entity]]:
    """返回 (同名公司, 近名公司列表)。

    近名用于拦「村田」与「村田制作所」被建成两份档案 —— 名称归一的前提是
    先别造出重复。单字名称不参与近名判断：`len(key) >= 2` 是刻意的，
    「华」这种单字会把一半公司都算成近名。
    """
    key = entity_names.key(name)
    if not key:
        return None, []
    exact: Entity | None = None
    similar: list[Entity] = []
    for ent in session.exec(select(Entity)).all():
        other = entity_names.key(ent.name)
        if not other:
            continue
        if other == key:
            exact = ent
        elif len(key) >= 2 and (key in other or other in key):
            similar.append(ent)
    return exact, similar


def find_entity_exact(session: Session, name: str) -> Entity | None:
    """只要同名公司，不要近名。客户归一这类场景用。"""
    return find_entity(session, name)[0]


def find_product(session: Session, entity_id: int, model: str) -> Product | None:
    """同公司内按型号查（忽略大小写与首尾空白）。"""
    target = (model or "").strip().casefold()
    if not target:
        return None
    for product in session.exec(
        select(Product).where(Product.entity_id == entity_id)
    ).all():
        if (product.model or "").strip().casefold() == target:
            return product
    return None
