"""插件声明式配置：schema 定义、全局/群级取值、校验与持久化。

插件在模块级声明字段::

    plugin.setting("welcome_template", type="text", label="欢迎语", default="欢迎 {nickname}")

处理事件时按当前会话读取（群级字段优先取群覆盖）::

    template = ctx.settings.get("welcome_template")
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("fllbot.plugin.settings")

MISSING: Any = object()
SCOPE_PREFIX = "plugin-settings"
MASK = "******"

VALID_TYPES = {"bool", "int", "float", "string", "text", "select", "multiselect", "list"}


@dataclass(frozen=True)
class SettingField:
    """一个配置字段的声明。"""

    key: str
    type: str = "string"
    label: str = ""
    default: Any = None
    description: str = ""
    options: tuple[Any, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    secret: bool = False
    group_scoped: bool = False

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("配置字段必须有 key")
        if self.type not in VALID_TYPES:
            raise ValueError(f"未知的配置类型 {self.type!r}（字段 {self.key}）")

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "type": self.type,
            "label": self.label or self.key,
            "default": self.default,
            "description": self.description,
            "options": list(self.options),
            "min": self.minimum,
            "max": self.maximum,
            "secret": self.secret,
            "group_scoped": self.group_scoped,
        }

    def coerce(self, value: Any) -> Any:
        """把外部输入转换成字段类型，非法时抛 ``ValueError``。"""
        if value is None:
            return None
        try:
            return self._coerce(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{self.label or self.key} 取值非法：{value!r}（{exc}）") from exc

    def _coerce(self, value: Any) -> Any:
        kind = self.type
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "on", "是", "开", "启用"}:
                return True
            if text in {"0", "false", "no", "off", "否", "关", "停用"}:
                return False
            raise ValueError("需要 true/false")
        if kind == "int":
            return self._clamp(int(value))
        if kind == "float":
            return self._clamp(float(value))
        if kind in {"string", "text"}:
            return str(value)
        if kind == "select":
            allowed = [str(item) for item in self.options]
            if not allowed:
                return str(value)
            text = str(value)
            if text not in allowed:
                raise ValueError(f"可选值：{', '.join(allowed)}")
            return text
        if kind in {"list", "multiselect"}:
            items = as_list(value)
            if kind == "multiselect" and self.options:
                allowed = {str(item) for item in self.options}
                invalid = [item for item in items if item not in allowed]
                if invalid:
                    raise ValueError(f"不在可选范围内：{', '.join(invalid)}")
            return items
        return value

    def _clamp(self, number: float | int) -> float | int:
        if self.minimum is not None and number < self.minimum:
            raise ValueError(f"不能小于 {self.minimum}")
        if self.maximum is not None and number > self.maximum:
            raise ValueError(f"不能大于 {self.maximum}")
        return number


def as_list(value: Any) -> list[str]:
    """把多行文本 / 逗号分隔字符串 / 列表统一成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [line.strip() for line in value.replace("\r", "\n").split("\n")]
        result: list[str] = []
        for line in items:
            result.extend(part.strip() for part in line.split(","))
        return [item for item in result if item]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("需要列表或逗号分隔的字符串")


class PluginSettings:
    """插件的配置集合，带内存缓存，读写都经过 SQLite 的 kv 表。"""

    def __init__(self, name_getter: Callable[[], str]) -> None:
        self._name_getter = name_getter
        self._fields: dict[str, SettingField] = {}
        self._values: dict[int, dict[str, Any]] = {}
        self._storage: Any = None

    # -- 声明 ------------------------------------------------------------- #
    @property
    def name(self) -> str:
        return self._name_getter() or "unnamed"

    @property
    def scope(self) -> str:
        return f"{SCOPE_PREFIX}:{self.name}"

    def define(self, setting: SettingField) -> SettingField:
        if setting.key in self._fields:
            raise ValueError(f"重复的配置字段：{setting.key}")
        self._fields[setting.key] = setting
        return setting

    def get_field(self, key: str) -> SettingField | None:
        return self._fields.get(key)

    def schema(self) -> list[dict[str, Any]]:
        return [self._fields[key].describe() for key in self._fields]

    @property
    def fields(self) -> dict[str, SettingField]:
        return dict(self._fields)

    # -- 持久化 ----------------------------------------------------------- #
    async def load(self, storage: Any) -> None:
        self._storage = storage
        self._values.clear()
        try:
            raw = await storage.kv_all(self.scope)
        except Exception:
            logger.exception("读取插件 %s 的配置失败", self.name)
            return
        for compound, value in raw.items():
            group_text, _, key = str(compound).partition(":")
            try:
                group_id = int(group_text)
            except ValueError:
                group_id = 0
                key = str(compound)
            self._values.setdefault(group_id, {})[key] = value

    # -- 读取 ------------------------------------------------------------- #
    def get(self, key: str, default: Any = MISSING, group_id: int | None = None) -> Any:
        setting = self._fields.get(key)
        fallback = default
        if fallback is MISSING:
            fallback = setting.default if setting is not None else None

        if group_id and setting is not None and setting.group_scoped:
            scoped = self._values.get(int(group_id), {})
            if key in scoped:
                return scoped[key]
        global_values = self._values.get(0, {})
        if key in global_values:
            return global_values[key]
        return fallback

    def values(self, group_id: int | None = None) -> dict[str, Any]:
        """返回当前生效的完整配置（默认值 + 全局 + 群覆盖）。"""
        result: dict[str, Any] = {setting.key: setting.default for setting in self._fields.values()}
        result.update(self._values.get(0, {}))
        if group_id:
            for key, value in self._values.get(int(group_id), {}).items():
                setting = self._fields.get(key)
                if setting is not None and setting.group_scoped:
                    result[key] = value
        return result

    def values_for_api(self, group_id: int | None = None) -> dict[str, Any]:
        """给管理端用的配置值，隐藏标记为 secret 的字段。"""
        values = self.values(group_id)
        for setting in self._fields.values():
            if setting.secret and values.get(setting.key):
                values[setting.key] = MASK
        return values

    # -- 写入 ------------------------------------------------------------- #
    async def set(self, key: str, value: Any, group_id: int | None = None) -> Any:
        setting = self._fields.get(key)
        coerced = setting.coerce(value) if setting is not None else value
        scope_id = int(group_id or 0)
        if setting is not None and not setting.group_scoped:
            scope_id = 0
        if self._storage is not None:
            await self._storage.kv_set(self.scope, f"{scope_id}:{key}", coerced)
        self._values.setdefault(scope_id, {})[key] = coerced
        return coerced

    async def update(self, values: dict[str, Any], group_id: int | None = None) -> dict[str, Any]:
        """批量写入；任一字段非法则整体不生效。"""
        prepared: list[tuple[str, Any]] = []
        for key, value in values.items():
            setting = self._fields.get(key)
            if setting is None:
                raise ValueError(f"未知的配置项：{key}")
            prepared.append((key, setting.coerce(value)))
        for key, value in prepared:
            await self.set(key, value, group_id)
        return self.values(group_id)

    def view(self, group_id: int | None) -> SettingsView:
        return SettingsView(self, group_id)


class SettingsView:
    """绑定到具体会话（群）的配置视图。"""

    def __init__(self, settings: PluginSettings, group_id: int | None) -> None:
        self._settings = settings
        self._group_id = int(group_id) if group_id else None

    def get(self, key: str, default: Any = MISSING) -> Any:
        return self._settings.get(key, default, group_id=self._group_id)

    def all(self) -> dict[str, Any]:
        return self._settings.values(self._group_id)

    def schema(self) -> list[dict[str, Any]]:
        return self._settings.schema()

    async def set(self, key: str, value: Any) -> Any:
        return await self._settings.set(key, value, group_id=self._group_id)

    @property
    def group_id(self) -> int | None:
        return self._group_id


__all__ = ["MASK", "PluginSettings", "SettingField", "SettingsView", "as_list"]
