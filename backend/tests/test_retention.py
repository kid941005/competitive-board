"""规格书文件的保留与清理（F-09）。

判断依据只有一条：**有没有参数引用它**。
被引用的删不得（删了溯源就断），没被引用也不能立刻删（用户可能还在复核）。

两种跑法都支持：pytest tests/ 或 python tests/test_retention.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-retention-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import datasheet_store
from app.config import DATASHEET_DIR
from app.db import engine, init_db
from app.models import Entity, Product, ProductSpec, SpecMetric, SpecSource
from sqlmodel import Session, select


def _drop(doc_id: str, *, ext: str = ".pdf", age_days: int = 0, size: int = 1024) -> Path:
    """造一份「上传过的规格书」：<doc_id>.pdf + <doc_id>.json。"""
    DATASHEET_DIR.mkdir(parents=True, exist_ok=True)
    payload = b"x" * size
    (DATASHEET_DIR / f"{doc_id}{ext}").write_bytes(payload)
    stamp = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    meta = DATASHEET_DIR / f"{doc_id}.json"
    meta.write_text(json.dumps({"filename": f"{doc_id}.pdf", "ext": ext,
                                "size": size, "uploaded_at": stamp}), encoding="utf-8")
    return meta


def _spec_referencing(doc_id: str) -> None:
    """建一条指向该文档的参数，模拟「已入库」。"""
    with Session(engine) as s:
        ent = s.exec(select(Entity)).first()
        if ent is None:
            ent = Entity(kind="competitor", name="保留测试")
            s.add(ent)
            s.flush()
        metric = s.exec(select(SpecMetric)).first()
        if metric is None:
            metric = SpecMetric(key="t", category="g", name="t", unit="",
                                value_type="number", better="na", precision=0)
            s.add(metric)
            s.flush()
        product = Product(entity_id=ent.id, model="RET-1", category="pump_air")
        s.add(product)
        s.flush()
        src = SpecSource(kind="datasheet", url=f"/datasheet/{doc_id}/file", title="上传的规格书")
        s.add(src)
        s.flush()
        s.add(ProductSpec(product_id=product.id, metric_id=metric.id,
                          value_num=1.0, source_id=src.id))
        s.commit()


def test_referenced_file_is_never_removed():
    init_db()
    _spec_referencing("aaaaaaaaaaaa")
    meta = _drop("aaaaaaaaaaaa", age_days=999)
    with Session(engine) as s:
        stats = datasheet_store.purge_orphans(s, keep_days=1)
    assert meta.exists(), "被参数引用的规格书不能删，删了溯源就断了"
    assert stats["referenced"] >= 1


def test_expired_orphan_is_removed_with_its_metadata():
    init_db()
    meta = _drop("bbbbbbbbbbbb", age_days=90)
    original = DATASHEET_DIR / "bbbbbbbbbbbb.pdf"
    with Session(engine) as s:
        stats = datasheet_store.purge_orphans(s, keep_days=30)
    assert not meta.exists() and not original.exists(), "过期且未被引用，应连原文一起删"
    assert stats["removed"] >= 1
    assert stats["bytes"] >= 1024


def test_fresh_orphan_is_kept_for_review():
    init_db()
    meta = _drop("cccccccccccc", age_days=0)
    with Session(engine) as s:
        datasheet_store.purge_orphans(s, keep_days=30)
    assert meta.exists(), "刚上传、还没提交复核的文档必须留着"


def test_keep_days_zero_disables_cleanup():
    init_db()
    meta = _drop("dddddddddddd", age_days=999)
    with Session(engine) as s:
        stats = datasheet_store.purge_orphans(s, keep_days=0)
    assert meta.exists() and stats["removed"] == 0


# ── 运行器 ─────────────────────────────────────────────────────────────────
def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}  {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}  {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
