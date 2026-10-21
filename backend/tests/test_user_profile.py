"""账号名与显示名的编辑（POST /admin/users/{uid}/profile）。

这一组是**流程测试**，顺序有意义：后面的用例依赖前面改过的名字，
所以运行器按显式 ORDER 执行，而不是字母序 —— 字母序会让「改名之前」的断言
在改名之后才跑，然后假失败。

    python tests/test_user_profile.py
    pytest tests/test_user_profile.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

_TMP = tempfile.mkdtemp(prefix="cib-profile-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, User
from fastapi.testclient import TestClient
from sqlmodel import Session, select

ADMIN_PW = "AdminPass123"
VIEWER_PW = "ViewerPass123"

_state: dict[str, object] = {}


# ── 夹具 ───────────────────────────────────────────────────────────────────
def _csrf(html: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "页面里没有 csrf-token meta"
    return m.group(1)


def _login(client: TestClient, username: str, password: str) -> int:
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    ).status_code


def _boot() -> tuple[TestClient, TestClient]:
    if _state.get("ready"):
        return _state["admin"], _state["viewer"]  # type: ignore[return-value]

    init_db()
    admin = TestClient(app)
    admin.__enter__()  # lifespan 建引导管理员
    assert _login(admin, "admin", ADMIN_PW) in (302, 303)

    token = _csrf(admin.get("/admin/users").text)
    r = admin.post(
        "/admin/users/create",
        data={"username": "viewer1", "password": VIEWER_PW, "display_name": "", "role": "viewer",
              "_csrf": token},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text[:300]

    viewer = TestClient(app)
    viewer.__enter__()
    assert _login(viewer, "viewer1", VIEWER_PW) in (302, 303)

    _state.update(ready=True, admin=admin, viewer=viewer)
    return admin, viewer


def _uid(username: str) -> int:
    with Session(engine) as s:
        row = s.exec(select(User).where(User.username == username)).first()
        assert row is not None, f"库里没有 {username}"
        return row.id  # type: ignore[return-value]


def _user(uid: int) -> tuple[str, str | None]:
    with Session(engine) as s:
        row = s.get(User, uid)
        assert row is not None
        return row.username, row.display_name


def _post_profile(client: TestClient, uid: int, username: str, display_name: str):
    # csrf 从 /compare 取：普通用户进不去 /admin/users（403 页没有 csrf meta），
    # 而校验顺序是「先 CSRF、后权限」，所以必须给一个合法 CSRF 才能真正测到权限拦截。
    token = _csrf(client.get("/compare").text)
    return client.post(
        f"/admin/users/{uid}/profile",
        data={"_csrf": token, "username": username, "display_name": display_name},
        follow_redirects=False,
    )


def _flash(resp) -> str:
    return unquote(resp.headers.get("location", ""))


def _user_audits(object_id: int) -> list[AuditLog]:
    with Session(engine) as s:
        return list(s.exec(
            select(AuditLog)
            .where(AuditLog.object_type == "user", AuditLog.object_id == object_id)
            .order_by(AuditLog.id)  # type: ignore[attr-defined]
        ).all())


# ── 用例（顺序有意义）─────────────────────────────────────────────────────
def test_rename_display_name_only():
    admin, _ = _boot()
    uid = _uid("viewer1")
    r = _post_profile(admin, uid, "viewer1", "研发小王")

    assert r.status_code == 303
    assert "ok=" in _flash(r), _flash(r)
    name, display = _user(uid)
    assert (name, display) == ("viewer1", "研发小王"), "只改显示名时账号名不能动"

    rows = _user_audits(uid)
    last = rows[-1]
    assert last.actor == "admin"
    assert "显示名" in last.changes and "账号" not in last.changes, last.changes


def test_rename_username_takes_effect_on_login():
    admin, _ = _boot()
    uid = _uid("viewer1")
    r = _post_profile(admin, uid, "wang", "研发小王")
    assert r.status_code == 303 and "ok=" in _flash(r), _flash(r)

    assert _user(uid)[0] == "wang"

    # 旧名失效、新名可登录 —— 这是「账号名就是登录名」的可验证后果
    fresh = TestClient(app)
    fresh.__enter__()
    assert _login(fresh, "viewer1", VIEWER_PW) == 401, "旧账号名不该还能登录"
    assert _login(fresh, "wang", VIEWER_PW) in (302, 303), "新账号名应该能登录"

    last = _user_audits(uid)[-1]
    assert "viewer1" in last.changes and "wang" in last.changes, last.changes
    assert last.object_label == "viewer1", "审计的 label 应留旧名，便于按旧名检索到这次变更"


def test_duplicate_username_rejected():
    admin, _ = _boot()
    uid = _uid("wang")
    r = _post_profile(admin, uid, "admin", "研发小王")

    assert r.status_code == 303
    assert "err=" in _flash(r), _flash(r)
    assert _user(uid)[0] == "wang", "重名被拒时不能改成功"


def test_invalid_username_rejected():
    admin, _ = _boot()
    uid = _uid("wang")
    for bad in ("ab", "有 空格", "a" * 33, "bad/name"):
        r = _post_profile(admin, uid, bad, "研发小王")
        assert "err=" in _flash(r), f"{bad!r} 应被拒：{_flash(r)}"
    assert _user(uid)[0] == "wang"


def test_no_change_writes_no_audit():
    admin, _ = _boot()
    uid = _uid("wang")
    before = len(_user_audits(uid))
    r = _post_profile(admin, uid, "wang", "研发小王")

    assert r.status_code == 303
    assert "没有变化" in _flash(r), _flash(r)
    assert len(_user_audits(uid)) == before, "没变化不该产生日志"


def test_viewer_cannot_rename():
    _, viewer = _boot()   # admin 这里用不到，只要 viewer 已登录
    uid = _uid("wang")
    r = _post_profile(viewer, uid, "hacked", "")
    # 页面层：无 admin:users 权限 → 403 页面（不是静默成功）
    assert r.status_code == 403, f"普通用户应被拒，实际 {r.status_code}"
    assert _user(uid)[0] == "wang"


def test_rename_self_keeps_session_and_audits_old_name():
    """最容易被写错的一条：actor 与 target 是同一行。

    若先改 target.username 再写审计，`actor` 会被记成新名字；
    且会话必须仍然有效（按 user_id 关联，不认账号名）。
    """
    admin, _ = _boot()
    uid = _uid("admin")
    r = _post_profile(admin, uid, "root", "超级管理员")
    assert r.status_code == 303 and "ok=" in _flash(r), _flash(r)

    # 会话没掉：改完还能进管理页
    assert admin.get("/admin/users").status_code == 200
    assert admin.get("/compare").status_code == 200

    name, display = _user(uid)
    assert (name, display) == ("root", "超级管理员")

    last = _user_audits(uid)[-1]
    assert last.actor == "admin", f"审计的 actor 必须是改名前的名字，实际 {last.actor!r}"
    assert last.object_label == "admin"
    assert '"admin"' in last.changes and '"root"' in last.changes


# ── 运行器：显式顺序 ───────────────────────────────────────────────────────
ORDER = [
    test_rename_display_name_only,
    test_rename_username_takes_effect_on_login,
    test_duplicate_username_rejected,
    test_invalid_username_rejected,
    test_no_change_writes_no_audit,
    test_viewer_cannot_rename,
    test_rename_self_keeps_session_and_audits_old_name,
]


def main() -> int:
    failed = 0
    for fn in ORDER:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}  {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}  {type(e).__name__}: {e}")
    print(f"\n{len(ORDER) - failed}/{len(ORDER)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
