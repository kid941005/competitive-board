"""液态玻璃样式层：链接顺序、令牌与组件覆盖必须真的存在。

为什么单开：视觉层没有 Python 行为可测，一旦 base.html 忘记引入、或新文件
被误清空，全站会静默退回旧样式，页面不会报错。这里钉住「引入了、内容在」。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-glass-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ["CIB_ADMIN_PASSWORD"] = "AdminPass123"
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app
from fastapi.testclient import TestClient

ADMIN_PW = "AdminPass123"


def _boot() -> TestClient:
    client = TestClient(app)
    client.__enter__()
    r = client.post("/login", data={"username": "admin", "password": ADMIN_PW},
                    follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    return client


def test_glass_layer_is_linked_after_app_css():
    c = _boot()
    html = c.get("/news").text
    app_pos = html.find("/static/app.css")
    glass_pos = html.find("/static/liquid-glass.css")
    assert app_pos >= 0 and glass_pos >= 0, "base.html 必须同时引入 app.css 与 liquid-glass.css"
    assert app_pos < glass_pos, "液态玻璃层必须排在 app.css 之后，才能覆盖设计令牌"


def test_glass_css_contains_tokens_and_component_overrides():
    css = Path(__file__).resolve().parent.parent / "app" / "static" / "liquid-glass.css"
    text = css.read_text(encoding="utf-8")
    for needle in (
        "--glass-bg:",
        ":root[data-theme=\"dark\"]",
        "--glass-blur:",
        ".news-body",
        ".logincard",
        "@supports not",
        "prefers-reduced-motion",
        "@media (max-width: 640px)",
        "@media (min-width: 1200px)",
        "max-width: none",
        ".news-timeline { width: 100%; max-width: none; margin: 0; }",
        ".news-day::before",
        "padding-left: 70px",
    ):
        assert needle in text, f"liquid-glass.css 缺少 {needle}"
    assert len(text) > 4000, "液态玻璃层不应是空壳"


def test_glass_css_is_served():
    c = _boot()
    r = c.get("/static/liquid-glass.css")
    assert r.status_code == 200
    assert "--glass-bg:" in r.text
    assert ".topbar" in r.text


if __name__ == "__main__":
    failed = 0
    for fn in (
        test_glass_layer_is_linked_after_app_css,
        test_glass_css_contains_tokens_and_component_overrides,
        test_glass_css_is_served,
    ):
        try:
            fn(); print(f"  PASS  {fn.__name__}")
        except Exception as exc:
            failed += 1; print(f"  FAIL  {fn.__name__}  {exc}")
    raise SystemExit(1 if failed else 0)
