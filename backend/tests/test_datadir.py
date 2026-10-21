"""数据目录自检的回归（部署时 `unable to open database file` 的可诊断性）。

为什么单独一个文件：这个缺陷只在**容器里**才暴露 —— 本机开发时目录属主是自己，
怎么写都成功；一旦镜像以 uid 10001 运行、宿主目录属主是 root，SQLite 只会丢一句
`unable to open database file`，分不清是路径错、没权限还是磁盘满，日志里还会
因为「失败后继续启动」把同一段 traceback 刷上两遍。

这组测试锁住四件事：

  1. 三种成因（目录建不出 / 不可写 / 磁盘满）各有各的话，不再共用一句原生报错
  2. 权限错误的正文必须带 uid、目录属主、权限位，以及可直接复制的修复命令
  3. 自检不留垃圾（探针文件写完要删掉）
  4. 路径存在但不是目录时，说「不是目录」而不是「打不开」

两种跑法都支持：pytest tests/ 或 python tests/test_datadir.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cib-datadir-test-")
os.environ["CIB_DATA_DIR"] = _TMP
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db


class _Patched:
    """临时替换类/模块属性，退出时还原（不引入 pytest 的 monkeypatch）。"""

    def __init__(self, target, name: str, replacement):
        self.target, self.name, self.replacement = target, name, replacement
        self.original = getattr(target, name)

    def __enter__(self):
        setattr(self.target, self.name, self.replacement)
        return self

    def __exit__(self, *_exc):
        setattr(self.target, self.name, self.original)
        return False


def _expect_error(fn, *needles: str) -> str:
    """执行 fn，要求抛 RuntimeError 且正文包含全部关键字，返回正文。"""
    try:
        fn()
    except RuntimeError as exc:
        msg = str(exc)
        for needle in needles:
            assert needle in msg, f"错误信息缺少「{needle}」，实际是：{msg}"
        return msg
    raise AssertionError("预期抛 RuntimeError，实际没有抛")


def _fresh_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="cib-dd-")) / "data"


# ── 1. 正常路径 ────────────────────────────────────────────────────────────
def test_writable_dir_passes_and_leaves_no_probe():
    d = _fresh_dir()
    db._diagnose_data_dir(d)
    assert d.is_dir(), "应当把目录建出来"
    leftovers = list(d.glob(".write-probe-*"))
    assert not leftovers, f"探针文件没清理：{leftovers}"


# ── 2. 不可写：必须有 uid、属主、修复命令 ─────────────────────────────────
def test_unwritable_dir_reports_uid_owner_and_fix():
    d = _fresh_dir()
    d.mkdir(parents=True)

    def deny(self, data=b""):
        raise PermissionError(13, "Permission denied")

    with _Patched(Path, "write_bytes", deny):
        _expect_error(lambda: db._diagnose_data_dir(d), "不可写", "uid=",
                      "目录属主", "chown", "docker volume")


# ── 3. 目录建不出来：区别于「不可写」 ─────────────────────────────────────
def test_missing_parent_not_creatable_says_so():
    d = _fresh_dir()

    def deny(self, parents=False, exist_ok=False):
        raise PermissionError(13, "Permission denied")

    with _Patched(Path, "mkdir", deny):
        msg = _expect_error(lambda: db._diagnose_data_dir(d), "无法创建")
    assert "不可写" not in msg, "建不出来和建出来但写不进去是两回事，不要混成一句"


# ── 4. 路径是文件 ──────────────────────────────────────────────────────────
def test_path_is_file_says_not_a_directory():
    d = _fresh_dir()
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_bytes(b"i am a file")

    _expect_error(lambda: db._diagnose_data_dir(d), "不是目录")


# ── 5. 磁盘满 ──────────────────────────────────────────────────────────────
def test_low_disk_reports_remaining_bytes():
    d = _fresh_dir()
    d.mkdir(parents=True)

    class _Usage:
        total = 10 * 1024 * 1024
        used = total - 1024
        free = 1024

    with _Patched(db.shutil, "disk_usage", lambda _p: _Usage()):
        _expect_error(lambda: db._diagnose_data_dir(d), "仅剩", "1024")


# ── 6. init_db 会顺手建出 datasheets 子目录 ───────────────────────────────
def test_init_db_creates_datasheets_subdir():
    from app.config import DATA_DIR

    db.init_db()
    sub = DATA_DIR / "datasheets"
    assert sub.is_dir(), f"init_db 应当建出 {sub}，否则首次上传规格书会失败"


# ── 运行器 ─────────────────────────────────────────────────────────────────
def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
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
