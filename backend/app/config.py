"""运行配置。开发默认 SQLite，生产用 PostgreSQL（与 deploy/docker-compose.yml 一致）。"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("CIB_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        # 兼容 SQLAlchemy 的 postgres:// 简写
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        return url
    return f"sqlite:///{(DATA_DIR / 'ciboard.db').as_posix()}"


# 单个对比页最多可勾选的产品数，避免表格横向失控
MAX_COMPARE = int(os.getenv("CIB_MAX_COMPARE", "8"))

# 数值比较的相对容差：差值小于 max(|a|,|b|) * EPS 视为相等
FLOAT_EPS = float(os.getenv("CIB_FLOAT_EPS", "1e-9"))

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

# 允许自动导入规格书的大类（当前与 CATEGORY_LABEL 一致，留出扩展位）
IMPORTABLE_CATEGORIES: tuple[str, ...] = tuple(CATEGORY_LABEL)


def category_label(code: str | None) -> str:
    if not code:
        return "未分类"
    return CATEGORY_LABEL.get(code, code)

