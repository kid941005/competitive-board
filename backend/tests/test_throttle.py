"""登录失败限速（F-13）。

守两件事：
1. 连续失败 5 次后开始指数退避，第 6 次尝试必须被 429 挡住；
2. **登录成功要把计数清零** —— 否则正常用户偶尔打错几次，会在某天突然被锁，
   而真正需要被挡的暴力尝试反而不受影响。

两种跑法都支持：pytest tests/ 或 python tests/test_throttle.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-throttle-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_USER"] = "admin"
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import throttle
from app.db import init_db
from app.main import app
from app.seed import seed
from fastapi.testclient import TestClient

ADMIN_PW = "AdminPass123"


def _client() -> TestClient:
    init_db()
    seed()
    c = TestClient(app)
    c.__enter__()
    return c


def _login(c: TestClient, password: str, username: str = "admin"):
    return c.post("/login", data={"username": username, "password": password},
                  follow_redirects=False)


# ── 单元级：计数与退避 ────────────────────────────────────────────────────
def test_backoff_starts_after_five_failures():
    throttle.reset_all()
    ip, user = "10.0.0.1", "admin"
    assert throttle.check(ip, user) == 0
    for i in range(4):
        assert throttle.fail(ip, user) == 0, f"第 {i + 1} 次失败还不该退避"
    wait = throttle.fail(ip, user)                    # 第 5 次
    assert wait > 0, "第 5 次失败后必须开始退避"
    assert throttle.check(ip, user) >= wait - 1
    # 继续失败，退避时间翻倍
    later = throttle.fail(ip, user)
    assert later >= wait, "退避应单调不减"


def test_success_clears_the_counter():
    throttle.reset_all()
    ip, user = "10.0.0.2", "admin"
    for _ in range(4):
        throttle.fail(ip, user)
    assert throttle.check(ip, user) == 0, "4 次还不该锁"
    throttle.succeed(ip, user)
    assert throttle.fail(ip, user) == 0, "成功之后重新计数，不应立刻触发退避"


def test_other_account_is_not_affected():
    throttle.reset_all()
    for _ in range(6):
        throttle.fail("10.0.0.3", "victim")
    assert throttle.check("10.0.0.3", "victim") > 0
    # 同一 IP 换账号仍在 IP 总量里，但没到 20 次之前不该误伤
    assert throttle.check("10.0.0.3", "someone-else") == 0


# ── 接口级：第 6 次登录被 429 ─────────────────────────────────────────────
def test_sixth_login_attempt_is_rate_limited():
    throttle.reset_all()
    c = _client()
    try:
        for _ in range(throttle.MAX_FAILURES):
            r = _login(c, "WrongPass123")
            assert r.status_code == 401, r.status_code
        blocked = _login(c, "WrongPass123")
        assert blocked.status_code == 429, f"应被限速挡住，实际 {blocked.status_code}"
        assert blocked.headers.get("Retry-After"), "429 要带 Retry-After"

        # 即便密码是对的，退避期内也不放行（否则限速形同虚设）
        blocked = _login(c, ADMIN_PW)
        assert blocked.status_code == 429
    finally:
        throttle.reset_all()


def test_successful_login_after_a_few_typos_is_not_blocked():
    throttle.reset_all()
    c = _client()
    try:
        for _ in range(throttle.MAX_FAILURES - 1):
            _login(c, "WrongPass123")
        r = _login(c, ADMIN_PW)
        assert r.status_code == 303, r.status_code
        # 清零后又能连续失败若干次而不被立刻锁
        assert throttle.check("testclient", "admin") == 0
    finally:
        throttle.reset_all()


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
