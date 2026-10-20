"""运行期可调的系统参数（管理员在界面改，立即生效）。

设计取舍：
- 只有登记在 SPECS 里的键才可写。**不接受任意键**，否则配置表会变成没人管得住的垃圾桶。
- 值统一以字符串存库，读取时按 kind 解析并做范围/枚举校验；解析失败回落默认值，
  保证一条脏数据不会让整站 500。
- 未写入的行不落库，回落 default —— 所以升级新增参数时不需要迁移脚本。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from . import config
from .models import AppSetting, utcnow


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    group: str
    kind: str  # int / float / str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    help: str = ""

    def parse(self, raw: Any) -> tuple[Any, str | None]:
        text = str(raw).strip()
        if self.kind == "int":
            try:
                value = int(text)
            except ValueError:
                return None, "必须是整数"
            if self.minimum is not None and value < self.minimum:
                return None, f"不能小于 {int(self.minimum)}"
            if self.maximum is not None and value > self.maximum:
                return None, f"不能大于 {int(self.maximum)}"
            return value, None

        if self.kind == "float":
            try:
                value = float(text)
            except ValueError:
                return None, "必须是数字"
            if self.minimum is not None and value < self.minimum:
                return None, f"不能小于 {self.minimum}"
            if self.maximum is not None and value > self.maximum:
                return None, f"不能大于 {self.maximum}"
            return value, None

        if self.choices and text not in self.choices:
            return None, "取值不在允许范围内"
        return text, None

    def serialize(self, value: Any) -> str:
        if self.kind == "float":
            return f"{float(value):g}"
        return str(value)


SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="compare.max_products",
        label="单页最多对比型号数",
        group="对比",
        kind="int",
        default=config.MAX_COMPARE,
        minimum=2,
        maximum=12,
        help="超过该数量时前端会拒绝继续勾选，接口也会截断。",
    ),
    SettingSpec(
        key="ui.default_category",
        label="默认展示的产品大类",
        group="对比",
        kind="str",
        default="pump_air",
        choices=tuple(config.CATEGORY_LABEL.keys()),
        help="打开对比页且未指定型号时，默认进入该大类。",
    ),
    SettingSpec(
        key="datasheet.max_upload_mb",
        label="规格书最大上传体积 (MB)",
        group="规格书",
        kind="int",
        default=int(os.getenv("CIB_MAX_UPLOAD_MB", "25")),
        minimum=1,
        maximum=200,
        help="超限会被接口直接拒绝，不会进入解析流程。",
    ),
    SettingSpec(
        key="datasheet.min_confidence",
        label="默认勾选的识别置信度阈值",
        group="规格书",
        kind="float",
        default=0.75,
        minimum=0.0,
        maximum=1.0,
        help="低于该值的识别结果默认不勾选，需人工确认。",
    ),
    SettingSpec(
        key="audit.retention_days",
        label="变更日志保留天数",
        group="安全",
        kind="int",
        default=int(os.getenv("CIB_AUDIT_DAYS", "180")),
        minimum=7,
        maximum=3650,
        help="启动时清理超期日志；0 不设为禁用，最小值 7 天以免误清。",
    ),
    SettingSpec(
        key="security.session_hours",
        label="登录会话有效期 (小时)",
        group="安全",
        kind="int",
        default=int(os.getenv("CIB_SESSION_HOURS", "12")),
        minimum=1,
        maximum=720,
        help="只影响新建立的会话；已登录用户的过期时间不回溯调整。",
    ),
)

SPEC_INDEX: dict[str, SettingSpec] = {spec.key: spec for spec in SPECS}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(spec.group for spec in SPECS))


def all_values(session: Session) -> dict[str, Any]:
    stored = {row.key: row.value for row in session.exec(select(AppSetting)).all()}
    out: dict[str, Any] = {}
    for spec in SPECS:
        raw = stored.get(spec.key)
        if raw is None:
            out[spec.key] = spec.default
            continue
        value, error = spec.parse(raw)
        # 脏数据不让整站挂掉：回落默认值
        out[spec.key] = spec.default if error else value
    return out


def get_value(session: Session, key: str) -> Any:
    spec = SPEC_INDEX[key]
    row = session.get(AppSetting, key)
    if row is None:
        return spec.default
    value, error = spec.parse(row.value)
    return spec.default if error else value


def update(session: Session, key: str, raw: Any, actor: str) -> tuple[bool, str | None]:
    spec = SPEC_INDEX.get(key)
    if spec is None:
        return False, "未知配置项"
    value, error = spec.parse(raw)
    if error:
        return False, f"{spec.label}：{error}"

    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=spec.serialize(value), updated_by=actor))
    else:
        row.value = spec.serialize(value)
        row.updated_at = utcnow()
        row.updated_by = actor
        session.add(row)
    session.commit()
    return True, None


def update_many(
    session: Session, values: dict[str, Any], actor: str
) -> tuple[int, list[str]]:
    """批量保存。逐项校验，有一项不合法就整体不落库，避免半套配置。"""
    errors: list[str] = []
    pending: list[tuple[SettingSpec, Any]] = []
    for key, raw in values.items():
        spec = SPEC_INDEX.get(key)
        if spec is None:
            errors.append(f"未知配置项：{key}")
            continue
        value, error = spec.parse(raw)
        if error:
            errors.append(f"{spec.label}：{error}")
            continue
        pending.append((spec, value))

    if errors:
        return 0, errors

    for spec, value in pending:
        row = session.get(AppSetting, spec.key)
        if row is None:
            session.add(AppSetting(key=spec.key, value=spec.serialize(value), updated_by=actor))
        else:
            row.value = spec.serialize(value)
            row.updated_at = utcnow()
            row.updated_by = actor
            session.add(row)
    session.commit()
    return len(pending), []


def snapshot(session: Session) -> list[dict]:
    """给管理页用：带当前值、默认值、是否已改、上次修改人。"""
    stored = {row.key: row for row in session.exec(select(AppSetting)).all()}
    current = all_values(session)
    out = []
    for spec in SPECS:
        row = stored.get(spec.key)
        out.append(
            {
                "spec": spec,
                "value": current[spec.key],
                "default": spec.default,
                "modified": row is not None and row.value != spec.serialize(spec.default),
                "updated_at": row.updated_at.isoformat()[:16] if row else None,
                "updated_by": row.updated_by if row else None,
            }
        )
    return out
