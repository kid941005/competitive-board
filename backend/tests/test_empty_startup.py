"""空库启动与「应用内置指标模板」的回归。

为什么要单独一个文件：v0.9.2 起默认 `CIB_SEED_DEMO=0`，部署出来是一个空库。
这个模式下有两件容易做错的事：

  1. **空库不等于可用**。对比页的每一行都来自指标字典，字典为空时
     导入进去也匹配不到任何指标 —— 页面必须明确告诉用户「先要一套字典」，
     而不是只显示一句「还没有产品数据」。
  2. **幂等判断的对象必须跟着模式变**。`seed()` 原来一律看「有没有实体」
     来判断要不要跳过；只写字典时库里永远没有实体，于是每点一次就再插
     17 个指标。现在按「这次要写的东西」判断。

覆盖：
  - 空库启动后只有表结构与管理员，没有任何业务数据
  - 空状态在「字典为空」与「有字典无产品」两种情况下说不同的话
  - 应用模板只写指标与适用大类，不创建任何公司/型号
  - 重复应用不会重复写入
  - 该动作要求管理员权限（普通用户 403）

两种跑法都支持：pytest tests/ 或 python tests/test_empty_startup.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-empty-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine
from app.main import app
from app.models import Entity, Product, SpecMetric
from app.seed import seed
from fastapi.testclient import TestClient
from sqlmodel import Session, select

_state: dict = {}


def _client() -> TestClient:
    """启动应用（lifespan 建表 + 建管理员），但不写任何示例数据。"""
    if "c" not in _state:
        c = TestClient(app)
        c.__enter__()
        _state["c"] = c
        r = c.post("/login", data={"username": "admin", "password": "AdminPass123"},
                   follow_redirects=False)
        assert r.status_code == 303, f"管理员登录失败：{r.status_code}"
    return _state["c"]  # type: ignore[return-value]


def _csrf(path: str = "/admin/settings") -> str:
    html = _client().get(path).text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, f"{path} 里没有 csrf-token"
    return m.group(1)


def _counts() -> tuple[int, int, int]:
    with Session(engine) as s:
        return (len(s.exec(select(SpecMetric)).all()),
                len(s.exec(select(Entity)).all()),
                len(s.exec(select(Product)).all()))


# ── 1. 空库启动 ────────────────────────────────────────────────────────────
def test_empty_db_has_only_tables_and_admin():
    c = _client()
    metrics, entities, products = _counts()
    assert (metrics, entities, products) == (0, 0, 0), \
        f"空库不该有业务数据，实际 指标={metrics} 公司={entities} 型号={products}"
    # 能登录就说明管理员已经自动建好（密码由 CIB_ADMIN_PASSWORD 指定）
    assert c.get("/compare").status_code == 200


# ── 2. 空状态说对话 ────────────────────────────────────────────────────────
def test_empty_state_tells_user_dictionary_is_missing():
    html = _client().get("/compare").text
    assert "这是一个空库" in html, "字典为空时必须点明，不能只说「还没有产品数据」"
    assert "/admin/settings" in html, "要给出可点的去处"
    assert "--metrics-only" in html, "命令行等价操作也要写出来（viewer 看得到）"
    assert "还没有任何产品数据" not in html, "字典为空和「有字典无产品」是两种情况，不要混"


# ── 3. 应用模板：只写字典 ─────────────────────────────────────────────────
def test_apply_template_writes_metrics_without_entities():
    c = _client()
    r = c.post("/admin/settings/apply-metrics-template",
               data={"_csrf": _csrf()}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    metrics, entities, products = _counts()
    assert metrics == 17, f"应写入 17 个指标（v3 合并风量与静压、删掉真空度后），实际 {metrics}"
    assert entities == 0, f"不该创建任何公司，实际 {entities}"
    assert products == 0, f"不该创建任何型号，实际 {products}"


# ── 4. 幂等：再点一次不重复写 ─────────────────────────────────────────────
def test_apply_template_is_idempotent():
    c = _client()
    before = _counts()[0]
    r = c.post("/admin/settings/apply-metrics-template",
               data={"_csrf": _csrf()}, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers.get("location", "")
    assert "err=" in loc, f"重复应用应给出「已有指标」提示，实际跳转 {loc}"
    assert _counts()[0] == before, "重复应用不该再插一批指标"


# ── 5. 有字典无产品时的文案 ───────────────────────────────────────────────
def test_state_with_dictionary_but_no_products():
    html = _client().get("/compare").text
    assert "还没有任何产品数据" in html
    assert "17 个指标" in html
    assert "这是一个空库" not in html, "字典已经在了，不该再说空库"


# ── 6. 幂等判断的对象要跟着模式走 ─────────────────────────────────────────
def test_metrics_only_gate_checks_metrics_not_entities():
    """库里已有指标、没有任何实体时，再跑 metrics_only 应当跳过。

    这是最容易回归的一条：gate 若仍按 Entity 判断，这个场景永远判不出
    「已写过」，点一次模板就多 17 个指标。
    """
    with Session(engine) as s:
        assert len(s.exec(select(Entity)).all()) == 0, "前提：这个场景下没有实体"
        assert len(s.exec(select(SpecMetric)).all()) > 0, "前提：已有指标"
    result = seed(metrics_only=True)
    assert result.get("skipped") == 1, f"应当跳过，实际 {result}"


# ── 7. 权限：普通用户不能应用模板 ─────────────────────────────────────────
def test_apply_template_requires_admin():
    c = _client()
    # 建一个 viewer 账号
    r = c.post("/admin/users/create", data={
        "_csrf": _csrf("/admin/users"),
        "username": "viewer_empty",
        "password": "ViewerPass123",
        "role": "viewer",
    }, follow_redirects=False)
    assert r.status_code == 303, f"建 viewer 失败：{r.status_code}"

    v = TestClient(app)
    v.__enter__()
    r = v.post("/login", data={"username": "viewer_empty", "password": "ViewerPass123"},
               follow_redirects=False)
    assert r.status_code == 303
    # 拿 viewer 的 csrf（设置页对 viewer 是 403，从任意页面的 meta 取）
    html = v.get("/compare").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "页面里应当有 csrf-token"
    r = v.post("/admin/settings/apply-metrics-template",
               data={"_csrf": m.group(1)}, follow_redirects=False)
    assert r.status_code == 403, f"普通用户应被拒，实际 {r.status_code}"
    v.__exit__(None, None, None)


# ── 运行器 ─────────────────────────────────────────────────────────────────
# 这 7 项是同一个流程的四步：空库 → 写字典 → 幂等 → 有字典。共用一份数据库，
# 所以**顺序有意义**，不能按字母序跑。pytest 默认按定义顺序收集，与下面一致；
# 单独跑其中某一项（如 pytest -k empty_state）会因为前置状态不满足而失败。
ORDER = [
    "test_empty_db_has_only_tables_and_admin",
    "test_empty_state_tells_user_dictionary_is_missing",
    "test_apply_template_writes_metrics_without_entities",
    "test_apply_template_is_idempotent",
    "test_state_with_dictionary_but_no_products",
    "test_metrics_only_gate_checks_metrics_not_entities",
    "test_apply_template_requires_admin",
]


def main() -> int:
    failed = 0
    for name in ORDER:
        fn = globals()[name]
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}  {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}  {type(e).__name__}: {e}")
    print(f"\n{len(ORDER) - failed}/{len(ORDER)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
