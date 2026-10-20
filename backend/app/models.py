"""数据模型。

设计取舍（P0）：一条 ProductSpec 对应 (产品, 指标) 一个值，测试条件以文本形式随值保存。
原因是「多维条件基数」会让横向对比的最优值计算复杂化，而 P0 阶段更需要结果可读、可校验。
需要多条件时，先把条件并入指标定义（如「插入损耗 @2.4GHz」），P1 再引入 condition_json 做透视。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Entity(SQLModel, table=True):
    """公司实体：本方 / 友商 / 客户。"""

    __tablename__ = "entities"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True, description="self / competitor / customer")
    name: str = Field(index=True)
    name_en: str | None = None
    region: str | None = None
    tier: str = Field(default="B", description="A / B / C，威胁或重要度分级")
    website: str | None = None
    note: str | None = None


class Product(SQLModel, table=True):
    """产品型号。竞品的每一个型号对应一行。"""

    __tablename__ = "products"

    id: int | None = Field(default=None, primary_key=True)
    entity_id: int = Field(foreign_key="entities.id", index=True)
    model: str = Field(index=True, description="型号，如 SX-1200")
    name: str | None = None
    category: str | None = Field(default=None, index=True, description="产品大类，如 声表滤波器")
    status: str = Field(default="active", description="active / eol / rumored")
    launch_date: date | None = None
    datasheet_url: str | None = None
    note: str | None = None


class SpecMetric(SQLModel, table=True):
    """指标字典 —— 本项目的核心资产。

    better 决定「最优值」的方向：
      higher = 越大越好，lower = 越小越好，target = 无优劣（不标最优），na = 纯文本不参与比较
    """

    __tablename__ = "spec_metrics"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True, description="稳定标识，导入时优先按此匹配")
    category: str = Field(index=True, description="指标组，用于页面分组与筛选")
    name: str
    unit: str = Field(default="")
    value_type: str = Field(default="number", description="number / text")
    better: str = Field(default="na", description="higher / lower / target / na")
    precision: int = Field(default=2, description="数值显示小数位")
    display_order: int = Field(default=100)
    is_key_metric: bool = Field(default=False, description="关键指标，对比页默认置顶")
    # 该指标适用于哪些产品大类（逗号分隔，如 "pump_air,pump_liquid"）。留空 = 全类别通用。
    # 压电陶瓷与压电泵的参数体系完全不同，不按类别过滤会出现大量无意义的空格。
    applies_to: str = Field(default="", description="适用产品大类，逗号分隔；空=通用")
    # 规格书里可能出现的其他写法，供自动识别用（逗号分隔）
    aliases: str = Field(default="", description="识别别名，逗号分隔")
    description: str | None = None


class SpecSource(SQLModel, table=True):
    """参数出处。每条参数都应能指回这里 —— 内部信任的基础。"""

    __tablename__ = "spec_sources"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(default="datasheet", description="datasheet / webpage / manual / api")
    url: str | None = None
    title: str | None = None
    captured_at: datetime = Field(default_factory=utcnow)
    note: str | None = None


class ProductSpec(SQLModel, table=True):
    """参数矩阵的一格。"""

    __tablename__ = "product_specs"

    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="products.id", index=True)
    metric_id: int = Field(foreign_key="spec_metrics.id", index=True)
    value_num: float | None = None
    value_text: str | None = None
    condition: str | None = Field(default=None, description="测试条件，如 @2.4GHz / 25℃")
    source_id: int | None = Field(default=None, foreign_key="spec_sources.id")
    confidence: float = Field(default=1.0, description="0~1，机器抽取低于阈值应进人工复核")
    verified: bool = Field(default=False, description="是否已人工确认")
    captured_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ── 账号与权限 ─────────────────────────────────────────────────────────────
class User(SQLModel, table=True):
    """系统账号。role 只有 admin / viewer 两种，权限映射见 auth.ROLE_PERMISSIONS。"""

    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    display_name: str | None = None
    password_hash: str
    role: str = Field(default="viewer", index=True, description="admin / viewer")
    is_active: bool = Field(default=True, description="停用后无法登录，会话立即失效")
    created_at: datetime = Field(default_factory=utcnow)
    created_by: str | None = None
    last_login_at: datetime | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthSession(SQLModel, table=True):
    """登录会话。只存 token 的哈希，泄库也无法直接冒用。"""

    __tablename__ = "auth_sessions"

    id: int | None = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    csrf: str
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime = Field(index=True)
    user_agent: str | None = None


class AppSetting(SQLModel, table=True):
    """运行期可调的系统参数。未写入的行回落 SettingSpec.default。"""

    __tablename__ = "app_settings"

    key: str = Field(primary_key=True)
    value: str
    updated_at: datetime = Field(default_factory=utcnow)
    updated_by: str | None = None
