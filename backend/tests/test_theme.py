"""主题切换与自适应：模板/样式/脚本三处必须对齐。

这组断言钉住的是「看起来能点、其实没生效」的高危点：
  · 下拉框三档在模板里，但 theme.js 没被引用 → 切了不记、换了不生效；
  · CSS 只认 data-theme，内联脚本没在样式表之前落值 → 深色用户白屏闪一下；
  · 窄屏断点删掉不会有任何页面报错，只能靠这里守住。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-theme-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_PASSWORD"] = "ThemePass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app
from fastapi.testclient import TestClient

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
ADMIN_PW = "ThemePass123"
_state: dict[str, object] = {}


def _client() -> TestClient:
    if _state.get("c"):
        return _state["c"]  # type: ignore[return-value]
    c = TestClient(app)
    c.__enter__()
    r = c.post("/login", data={"username": "admin", "password": ADMIN_PW},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    _state["c"] = c
    return c


def test_header_has_three_theme_options():
    html = _client().get("/news").text
    assert 'id="themeSelect"' in html
    assert '<option value="auto">跟随系统</option>' in html
    assert '<option value="light">浅色</option>' in html
    assert '<option value="dark">深色</option>' in html


def test_inline_script_sets_theme_before_stylesheet():
    """内联脚本必须在 app.css 之前，否则深色首屏会先白一帧。"""
    html = _client().get("/news").text
    assert 'localStorage.getItem("cib-theme")' in html
    assert 'setAttribute("data-theme"' in html
    assert html.index("cib-theme") < html.index("/static/app.css")


def test_theme_script_is_loaded():
    html = _client().get("/news").text
    assert "/static/theme.js?v=" in html


def test_css_scopes_dark_to_data_theme_and_has_mobile_breakpoint():
    text = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ':root[data-theme="dark"]' in text
    assert ':root[data-theme="auto"]' in text
    assert "color-scheme: dark" in text
    assert "@media (max-width: 640px)" in text
    # 手机断点里导航必须换行到独立一行
    block = text[text.index("@media (max-width: 640px)"):]
    assert ".mainnav" in block


def test_theme_script_contract():
    js = (STATIC / "theme.js").read_text(encoding="utf-8")
    for needle in ("cib-theme", "matchMedia", "data-theme-pref",
                   'setAttribute("data-theme"', "localStorage.setItem"):
        assert needle in js, needle


def main() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failed += 1
                print(f"  FAIL  {name}  {exc}")
            except Exception as exc:
                failed += 1
                print(f"  ERROR {name}  {type(exc).__name__}: {exc}")
    print(f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
