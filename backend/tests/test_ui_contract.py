"""页面渲染契约：模板用到的变量，路由必须给。

为什么单开一组：Jinja 默认的 `Undefined` 会把「漏传变量」静默渲染成空字符串 ——
不报错、不告警，只是页面上该出现的东西没出现。`/admin/logs` 清理完日志看不到
「已清理 N 条」就是这么来的：路由不接 `?ok=`，模板里却有 `{% if ok %}`。
`forbidden.html` 的「重新登录」同理。

这里把模板环境临时换成 `StrictUndefined`，让漏传直接变成 500。凡是本文件能通过的页面，
都说明「模板引用的每个变量都在 context 里」，或者显式用 `is defined` 声明为可选。

    python tests/test_ui_contract.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-ui-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import web_shared
from app.db import init_db
from app.main import app
from app.seed import seed
from fastapi.testclient import TestClient
from jinja2 import StrictUndefined

ADMIN_PW = "AdminPass123"
VIEWER_PW = "ViewerPass123"
_state: dict[str, object] = {}


def _boot(*, seed_data: bool) -> TestClient:
    """准备一个有数据的实例（只建一次），并把模板环境换成严格模式。"""
    if _state.get("c"):
        return _state["c"]  # type: ignore[return-value]
    init_db()
    web_shared.templates.env.undefined = StrictUndefined
    client = TestClient(app)
    client.__enter__()
    if seed_data:
        seed()
    r = client.post("/login", data={"username": "admin", "password": ADMIN_PW},
                    follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    _state["c"] = client
    return client


def _viewer() -> TestClient:
    """建一个普通用户并登录（拿不到管理员权限才能测 403 页）。"""
    if _state.get("v"):
        return _state["v"]  # type: ignore[return-value]
    admin = _boot(seed_data=True)
    html = admin.get("/admin/users").text
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    admin.post("/admin/users/create",
               data={"username": "viewer1", "password": VIEWER_PW,
                     "display_name": "", "role": "viewer", "_csrf": csrf})
    v = TestClient(app)
    v.__enter__()
    r = v.post("/login", data={"username": "viewer1", "password": VIEWER_PW},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    _state["v"] = v
    return v


# ── 严格模式下的全页面渲染 ─────────────────────────────────────────────────
def test_empty_db_pages_render():
    """空库走 empty.html —— 它有自己的变量集合，最容易漏。"""
    c = _boot(seed_data=False)
    for path in ("/compare", "/products", "/admin/logs", "/admin/settings"):
        r = c.get(path)
        assert r.status_code < 500, f"{path} 在严格模式下 500"


def test_pages_render_under_strict_undefined():
    """核心用例：有数据时所有页面都必须渲染成功。"""
    c = _boot(seed_data=True)
    pages = ["/compare", "/products", "/products/1", "/datasheet", "/import",
             "/admin/users", "/admin/tokens", "/admin/logs", "/admin/settings"]
    bad = []
    for path in pages:
        r = c.get(path)
        if r.status_code >= 500:
            bad.append((path, r.status_code))
    assert not bad, f"以下页面引用了 context 里没有的变量：{bad}"


def test_footer_shows_app_version_everywhere():
    """页脚版本号必须在**所有**页面出现，且等于 app.version。

    它来自模板全局（main.py 里与 app.version 同源），所以登录页（没有 _ctx）也得有；
    这正是最容易漏的一个渲染点。
    """
    from app.main import app as _app

    c = _boot(seed_data=True)
    login = c.get("/login")          # 未登录页也要有
    pages = [("/login", login.text)]
    for path in ("/compare", "/products", "/import", "/admin/settings"):
        pages.append((path, c.get(path).text))

    for name, html in pages:
        assert '<footer class="appfoot">' in html, f"{name} 缺页脚"
        assert f'v{_app.version}' in html, f"{name} 页脚版本号不是 v{_app.version}"


def test_not_found_page_renders():
    c = _boot(seed_data=True)
    r = c.get("/products/99999")
    assert r.status_code == 404, r.status_code
    assert r.status_code < 500


def test_login_failure_branch_renders():
    """登录失败的 401 分支单独渲染 login.html，曾经漏传 csrf。"""
    c = _boot(seed_data=True)
    r = c.post("/login", data={"username": "admin", "password": "DefinitelyWrong1"},
               follow_redirects=False)
    assert r.status_code == 401, r.status_code
    assert "账号或密码错误" in r.text


def test_forbidden_page_renders_without_optional_vars():
    """403 页的 hint / user 是可选变量，权限不足时必须能渲染出来。"""
    v = _viewer()
    for path in ("/admin/users", "/admin/settings", "/admin/logs", "/admin/tokens"):
        r = v.get(path, follow_redirects=False)
        assert r.status_code == 403, f"{path} 应 403，实际 {r.status_code}"
        assert "权限不足" in r.text


def test_csrf_failure_page_offers_relogin():
    """会话校验失败时，「重新登录」入口必须出现 —— 它依赖的 user 变量曾是未定义的。"""
    v = _viewer()
    r = v.post("/admin/users/create",
               data={"username": "x1", "password": "Abcdefg123", "display_name": "",
                     "role": "viewer", "_csrf": "not-the-right-one"},
               follow_redirects=False)
    assert r.status_code == 403
    assert "重新登录" in r.text, "CSRF 失效页应给重新登录入口"


# ── flash 提示必须真的显示出来 ─────────────────────────────────────────────
def test_admin_logs_shows_flash():
    """清理日志后重定向回 /admin/logs?ok=…，页面要把它显示出来。

    回归：`admin_logs_page` 曾经不接 ok/err 参数，模板里的 {% if ok %} 永远为假，
    于是「已清理 N 条」这句提示从来没出现过。
    """
    c = _boot(seed_data=True)
    r = c.get("/admin/logs", params={"ok": "已清理 42 条超过 180 天的日志"})
    assert r.status_code == 200
    assert "已清理 42 条" in r.text, "清理日志的成功提示没有渲染出来"

    r = c.get("/admin/logs", params={"err": "筛选条件不合法"})
    assert "筛选条件不合法" in r.text


def test_purge_then_redirect_carries_message():
    """端到端：POST 清理 → 303 带 ?ok= → 跟随后页面能看到。"""
    c = _boot(seed_data=True)
    html = c.get("/admin/logs").text
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    r = c.post("/admin/logs/purge", data={"_csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert "ok=" in location, location

    followed = c.get("/admin/logs" + location[location.index("?"):])
    assert "已清理" in followed.text


ORDER = [
    test_empty_db_pages_render,
    test_pages_render_under_strict_undefined,
    test_not_found_page_renders,
    test_login_failure_branch_renders,
    test_forbidden_page_renders_without_optional_vars,
    test_csrf_failure_page_offers_relogin,
    test_admin_logs_shows_flash,
    test_purge_then_redirect_carries_message,
    test_footer_shows_app_version_everywhere,
]


def main() -> int:
    failed = 0
    for fn in ORDER:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}  {exc}")
        except Exception as exc:
            failed += 1
            print(f"  ERROR {fn.__name__}  {type(exc).__name__}: {exc}")
    print(f"\n{len(ORDER) - failed}/{len(ORDER)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
