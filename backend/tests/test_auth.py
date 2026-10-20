"""权限体系单测：密码、角色边界、页面层与接口层的拦截、CSRF、系统参数生效。

两种跑法都支持：
    pytest tests/
    python tests/test_auth.py

测试使用独立的临时数据目录，不碰开发库。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# 必须在导入 app 之前设置，config 在导入期读环境变量
_TMP = tempfile.mkdtemp(prefix="cib-auth-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import auth, settings_store  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.seed import seed  # noqa: E402

ADMIN_PW = "AdminPass123"
VIEWER_PW = "ViewerPass123"
VIEWER_NAME = "viewer1"

_state: dict[str, object] = {}


# ── 夹具 ───────────────────────────────────────────────────────────────────
def _boot():
    if _state.get("ready"):
        return _state["admin"], _state["viewer"]
    init_db()
    seed()

    admin = TestClient(app)
    admin.__enter__()  # 触发 lifespan，创建引导管理员
    _login(admin, "admin", ADMIN_PW)
    csrf = _csrf(admin)

    viewer = TestClient(app)
    viewer.__enter__()
    r = admin.post(
        "/admin/users/create",
        data={
            "username": VIEWER_NAME,
            "password": VIEWER_PW,
            "display_name": "只读用户",
            "role": "viewer",
            "_csrf": csrf,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, f"创建普通用户失败：{r.status_code} {r.text[:200]}"

    _state.update(admin=admin, viewer=viewer, ready=True)
    return admin, viewer


def _login(client: TestClient, username: str, password: str):
    r = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"登录失败：{r.status_code} {r.text[:200]}"
    return r


def _csrf(client: TestClient) -> str:
    html = client.get("/compare").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "页面里没有 csrf meta"
    return m.group(1)


def _fresh_admin():
    admin, _ = _boot()
    _login(admin, "admin", ADMIN_PW)
    return admin


def _fresh_viewer():
    _, viewer = _boot()
    _login(viewer, VIEWER_NAME, VIEWER_PW)
    return viewer


# ── 纯逻辑：密码 ───────────────────────────────────────────────────────────
def test_password_roundtrip():
    h = auth.hash_password("Abcdef12345")
    assert h.startswith("pbkdf2_sha256$")
    assert auth.verify_password("Abcdef12345", h) is True
    assert auth.verify_password("Abcdef12346", h) is False


def test_password_hash_is_salted():
    """同一密码两次哈希必须不同，否则说明没加盐。"""
    assert auth.hash_password("SamePass123") != auth.hash_password("SamePass123")


def test_password_policy():
    assert auth.check_password_policy("short1") is not None          # 太短
    assert auth.check_password_policy("alllettersonly") is not None  # 无数字
    assert auth.check_password_policy("1234567890") is not None      # 无字母
    assert auth.check_password_policy(" Admin12345") is not None     # 首尾空白
    assert auth.check_password_policy("GoodPass123") is None


def test_verify_rejects_garbage_hash():
    assert auth.verify_password("x", "") is False
    assert auth.verify_password("x", "not-a-hash") is False


# ── 纯逻辑：角色边界 ───────────────────────────────────────────────────────
def test_role_permission_boundary():
    admin = auth.ROLE_PERMISSIONS["admin"]
    viewer = auth.ROLE_PERMISSIONS["viewer"]

    assert auth.P_VIEW in viewer and auth.P_EXPORT in viewer
    # 普通用户不得拥有任何写权限或管理权限
    for perm in (auth.P_WRITE, auth.P_USERS, auth.P_SETTINGS):
        assert perm not in viewer, f"viewer 不应拥有 {perm}"
        assert perm in admin, f"admin 应拥有 {perm}"


def test_unknown_role_has_no_permission():
    ghost = User(id=999, username="ghost", password_hash="x", role="unknown")
    assert auth.permissions_for(ghost) == frozenset()


# ── 纯逻辑：系统参数校验 ───────────────────────────────────────────────────
def test_setting_spec_validation():
    spec = settings_store.SPEC_INDEX["compare.max_products"]
    assert spec.parse("6")[0] == 6
    assert spec.parse("abc")[1] is not None        # 非整数
    assert spec.parse("1")[1] is not None          # 低于下限
    assert spec.parse("99")[1] is not None         # 高于上限


def test_setting_choices_validation():
    spec = settings_store.SPEC_INDEX["ui.default_category"]
    assert spec.parse("pump_air")[1] is None
    assert spec.parse("not-a-category")[1] is not None


def test_unknown_setting_rejected():
    init_db()
    with Session(engine) as s:
        ok, err = settings_store.update(s, "system.rm_rf", "1", "tester")
    assert ok is False and err


# ── 页面层：未登录 ─────────────────────────────────────────────────────────
def test_anonymous_redirected_to_login():
    _boot()
    anon = TestClient(app)
    anon.__enter__()
    for path in ("/compare", "/datasheet", "/admin/users", "/admin/settings"):
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303, f"{path} 应跳登录，实际 {r.status_code}"
        assert r.headers["location"].startswith("/login"), path


def test_anonymous_api_gets_401():
    _boot()
    anon = TestClient(app)
    anon.__enter__()
    for path in ("/api/matrix", "/api/metrics", "/api/products"):
        r = anon.get(path)
        assert r.status_code == 401, f"{path} 应 401，实际 {r.status_code}"
        assert r.json()["code"] == "unauthenticated"


# ── 页面层：管理员全通 ─────────────────────────────────────────────────────
def test_admin_reaches_everything():
    admin = _fresh_admin()
    for path in ("/compare", "/datasheet", "/admin/users", "/admin/settings"):
        r = admin.get(path)
        assert r.status_code == 200, f"{path} 管理员应可访问，实际 {r.status_code}"


def test_admin_sees_admin_nav():
    admin = _fresh_admin()
    html = admin.get("/compare").text
    assert "/admin/users" in html and "/admin/settings" in html
    assert "管理员" in html


# ── 页面层：普通用户被挡 ───────────────────────────────────────────────────
def test_viewer_can_view_compare():
    viewer = _fresh_viewer()
    assert viewer.get("/compare").status_code == 200
    assert viewer.get("/api/matrix").status_code == 200


def test_viewer_hidden_from_write_and_admin_nav():
    """界面层：普通用户看不到写操作与管理入口。"""
    viewer = _fresh_viewer()
    html = viewer.get("/compare").text
    assert "/admin/users" not in html
    assert "/admin/settings" not in html
    assert "/datasheet" not in html
    assert "普通用户" in html


def test_viewer_gets_403_page_on_admin_routes():
    """界面层：直访管理 URL 得到 403 页面，而不是被静默重定向。"""
    viewer = _fresh_viewer()
    for path in ("/admin/users", "/admin/settings", "/datasheet"):
        r = viewer.get(path)
        assert r.status_code == 403, f"{path} 应 403，实际 {r.status_code}"
        assert "权限不足" in r.text


# ── 接口层：普通用户被挡 ───────────────────────────────────────────────────
def test_viewer_403_on_admin_api():
    """接口层：普通用户调用管理接口一律 403。

    注意两类端点的错误形态不同，这是有意为之：
      /api/*      → JSON 403（供程序判断）
      表单端点    → HTML 403 页面（供人看）
    """
    viewer = _fresh_viewer()
    csrf = _csrf(viewer)

    # 1) 纯接口：JSON 403
    for path, payload in (
        (
            "/api/datasheet/commit",
            {
                "doc_id": "0" * 12,
                "target": {"category": "pump_air", "model": "X-1"},
                "rows": [],
            },
        ),
    ):
        r = viewer.post(path, json=payload, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 403, f"{path} 应 403，实际 {r.status_code}"
        assert r.json()["code"] == "forbidden"

    r = viewer.post("/api/import", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"

    # 2) 表单端点：HTML 403 页面
    for path, data in (
        ("/admin/users/create", {"username": "x1", "password": "Whatever123", "role": "admin"}),
        ("/admin/settings", {"compare.max_products": "12"}),
    ):
        r = viewer.post(path, data={**data, "_csrf": csrf}, follow_redirects=False)
        assert r.status_code == 403, f"{path} 应 403，实际 {r.status_code}"
        assert "权限不足" in r.text


def test_viewer_export_allowed():
    """导出属于查看能力，普通用户可用。"""
    viewer = _fresh_viewer()
    assert viewer.get("/export.csv").status_code == 200


# ── CSRF ───────────────────────────────────────────────────────────────────
def test_admin_write_without_csrf_rejected():
    admin = _fresh_admin()
    r = admin.post(
        "/admin/users/create",
        data={"username": "nocsrf", "password": "Whatever123", "role": "viewer"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "csrf" in r.text.lower() or "会话校验失败" in r.text


def test_admin_write_with_csrf_works():
    admin = _fresh_admin()
    csrf = _csrf(admin)
    r = admin.post(
        "/admin/users/create",
        data={
            "username": "withcsrf",
            "password": "Whatever123",
            "role": "viewer",
            "_csrf": csrf,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "err=" not in r.headers["location"]


# ── 防锁死 ─────────────────────────────────────────────────────────────────
def test_cannot_demote_self():
    admin = _fresh_admin()
    csrf = _csrf(admin)
    with Session(engine) as s:
        me = s.exec(select(User).where(User.username == "admin")).first()
        uid = me.id
    r = admin.post(
        f"/admin/users/{uid}/role",
        data={"role": "viewer", "_csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "err=" in r.headers["location"]
    with Session(engine) as s:
        assert s.get(User, uid).role == "admin"


def test_cannot_disable_self():
    admin = _fresh_admin()
    csrf = _csrf(admin)
    with Session(engine) as s:
        uid = s.exec(select(User).where(User.username == "admin")).first().id
    r = admin.post(f"/admin/users/{uid}/toggle", data={"_csrf": csrf}, follow_redirects=False)
    assert "err=" in r.headers["location"]


# ── 停用即时生效 ───────────────────────────────────────────────────────────
def test_disabled_user_session_invalidated():
    admin = _fresh_admin()
    csrf = _csrf(admin)
    with Session(engine) as s:
        viewer = s.exec(select(User).where(User.username == VIEWER_NAME)).first()
        uid = viewer.id

    viewer_client = TestClient(app)
    viewer_client.__enter__()
    _login(viewer_client, VIEWER_NAME, VIEWER_PW)
    assert viewer_client.get("/api/matrix").status_code == 200

    admin.post(f"/admin/users/{uid}/toggle", data={"_csrf": csrf}, follow_redirects=False)
    # 会话已被吊销，旧 cookie 立即失效
    assert viewer_client.get("/api/matrix").status_code == 401

    # 恢复，避免影响后续用例
    admin.post(f"/admin/users/{uid}/toggle", data={"_csrf": csrf}, follow_redirects=False)


# ── 系统参数实时生效 ───────────────────────────────────────────────────────
def test_setting_change_takes_effect():
    admin = _fresh_admin()
    csrf = _csrf(admin)
    r = admin.post(
        "/admin/settings",
        data={"compare.max_products": "3", "_csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers["location"]

    data = admin.get("/api/matrix").json()
    assert len(data["products"]) <= 3, f"应被截断到 3 个，实际 {len(data['products'])}"

    # 还原
    admin.post(
        "/admin/settings",
        data={"compare.max_products": "8", "_csrf": csrf},
        follow_redirects=False,
    )


def test_invalid_setting_revealed_not_saved():
    admin = _fresh_admin()
    csrf = _csrf(admin)
    r = admin.post(
        "/admin/settings",
        data={"compare.max_products": "999", "_csrf": csrf},
        follow_redirects=False,
    )
    assert "err=" in r.headers["location"]
    with Session(engine) as s:
        assert settings_store.get_value(s, "compare.max_products") == 8


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
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"  ERROR {name}  {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
