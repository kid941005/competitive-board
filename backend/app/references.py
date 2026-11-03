"""规格来源的引用计数与安全清理。

来源行可能被多个参数或多个客户采用记录共用；删除某个型号时不能按
「它刚被删过的参数」直接清理，否则会删掉其他型号仍在用的溯源信息。
"""

from __future__ import annotations

from sqlmodel import Session, select

from .models import Adoption, ProductSpec, SpecSource


def delete_unreferenced_sources(session: Session, source_ids: set[int]) -> int:
    """删除给定来源中已没有任何参数/采用记录引用的行，返回实际删除数。

    调用方应在删除 ProductSpec/Adoption 后、提交事务前调用。查询必须发生在
    删除后的同一事务里，这样并发提交也只会看到一致快照。
    """
    if not source_ids:
        return 0

    specs = session.exec(
        select(ProductSpec.source_id).where(ProductSpec.source_id.in_(source_ids))
    ).all()
    adoptions = session.exec(
        select(Adoption.source_id).where(Adoption.source_id.in_(source_ids))
    ).all()
    still_referenced = {sid for sid in (*specs, *adoptions) if sid is not None}

    removed = 0
    for source_id in source_ids - still_referenced:
        source = session.get(SpecSource, source_id)
        if source is not None:
            session.delete(source)
            removed += 1
    return removed
