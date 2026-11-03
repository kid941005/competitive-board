"""运行配置。开发默认 SQLite，生产用 PostgreSQL（与 deploy/docker-compose.yml 一致）。"""

from __future__ import annotations

import os
from pathlib import Path


def env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """读取有范围的整数环境变量；非法值回落默认值，而不是在 import 阶段崩掉。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return min(max(value, minimum), maximum)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("CIB_DATA_DIR", BASE_DIR / "data"))

# 目录的创建与可写性检查交给 db.init_db()：那里能拿到当前 uid 与目录属主，
# 报错才说得清是「路径不对」还是「没有写权限」。
# 这里只做一次尽力而为的创建，失败不抛 —— 否则容器里会变成 import 阶段
# 一句 PermissionError，比 SQLite 的 unable to open database file 更难查。
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

# 规格书原文与解析结果落在这里：<doc_id>.<ext> + <doc_id>.json。
# 单独成常量是因为「写入」（页面路由）与「清理」（datasheet_store）都要用到它。
DATASHEET_DIR = DATA_DIR / "datasheets"


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        # 兼容 SQLAlchemy 的 postgres:// 简写
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        return url
    return f"sqlite:///{(DATA_DIR / 'ciboard.db').as_posix()}"


# 单个对比页最多可勾选的产品数，避免表格横向失控
MAX_COMPARE = env_int("CIB_MAX_COMPARE", 8, minimum=2, maximum=12)
# 幂等记录保留天数：请求指纹与首次响应会随每天的重试累积，不能永久留存
IDEMPOTENCY_KEEP_DAYS = env_int("CIB_IDEMPOTENCY_DAYS", 30, minimum=1, maximum=3650)
# JSON 导入单次最多行数，避免一次请求占满内存和数据库事务
IMPORT_MAX_ROWS = env_int("CIB_IMPORT_MAX_ROWS", 2000, minimum=1, maximum=10000)
# 解析层的资源上限。上传字节数只是第一道门：一个 25 MB 的 PDF 可以有上千页，
# 一个压缩率很高的 XLSX 也可能展开成远超原始大小的 XML。若不在这里再设门，
# 「文件不大」仍可能耗尽内存或让单次请求长时间占住工作线程。
DATASHEET_MAX_PAGES = env_int("CIB_DATASHEET_MAX_PAGES", 300, minimum=1, maximum=5000)
DATASHEET_MAX_CHARS = env_int(
    "CIB_DATASHEET_MAX_CHARS", 2_000_000, minimum=10_000, maximum=20_000_000
)
SHEET_MAX_CELLS = env_int("CIB_SHEET_MAX_CELLS", 1_000_000, minimum=1000, maximum=5_000_000)
SHEET_MAX_UNCOMPRESSED_BYTES = env_int(
    "CIB_SHEET_MAX_UNCOMPRESSED_BYTES", 100 * 1024 * 1024,
    minimum=1024 * 1024, maximum=1024 * 1024 * 1024,
)

# HTTPS 部署时必须置 1，否则会话 Cookie 会在明文链路上传输
COOKIE_SECURE = os.getenv("CIB_COOKIE_SECURE", "0").strip().lower() in ("1", "true", "on", "yes")

# 产品大类代码 → 中文名。指标字典的 applies_to 用的就是这些代码。
# 只收「器件」类：规格书是某款器件的参数页，导入流程按型号入库。
# 压电陶瓷属于材料牌号（典型值表），与器件 datasheet 形态不同，故不在此列。
CATEGORY_LABEL: dict[str, str] = {
    "pump_air": "压电气泵",
    "fan": "压电风扇",
    "pump_liquid": "压电液泵",
}

# 自家公司名。页面上直接用这个名字，不再用「本方 / 我方 / 本公司」这类代称。
SELF_COMPANY_NAME = "左蓝微"
# 历史版本用过的占位名，启动时会被替换成 SELF_COMPANY_NAME（老数据卷兼容）
SELF_LEGACY_NAMES: tuple[str, ...] = ("本公司", "本方", "我方")

def category_label(code: str | None) -> str:
    if not code:
        return "未分类"
    return CATEGORY_LABEL.get(code, code)

