"""单群策略：统一继承规则，旧插件配置和群配置继续作为默认值。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from .app import FLLBot

logger = logging.getLogger("fllbot.policy")


class GroupPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=80)
    auto_group: bool | None = None
    trigger_mode: Literal["at", "prefix", "all"] | None = None
    trigger_modes: list[Literal["at", "prefix", "all"]] | None = Field(default=None, min_length=1, max_length=3)
    trigger_prefix: str | None = Field(default=None, max_length=80)
    cooldown_seconds: int | None = Field(default=None, ge=0, le=120)
    max_tool_rounds: int | None = Field(default=None, ge=1, le=5)
    allowed_tools: list[str] | None = Field(default=None, max_length=256)
    provider: str = Field(default="", max_length=80)
    system_extra: str = Field(default="", max_length=4000)


def tool_index(bot: FLLBot) -> dict[str, dict[str, Any]]:
    """当前所有插件显式开放的 AI 工具：工具 ID -> 展示信息。"""
    result: dict[str, dict[str, Any]] = {}
    for plugin in bot.plugins.loaded():
        if plugin.name == "agent":
            continue
        for spec in plugin.ai_tools:
            result[spec.qualified_name] = {
                "id": spec.qualified_name,
                "title": spec.display_title,
                "plugin": plugin.name,
                "description": spec.description,
                "permission": spec.permission,
            }
    return result


def expand_tool_names(bot: FLLBot, names: Any) -> list[str]:
    """把工具 ID（``插件名__工具名``）或旧的插件名展开成工具 ID 列表。"""
    catalog = tool_index(bot)
    result: list[str] = []
    for raw in names or ():
        name = str(raw).strip()
        if not name:
            continue
        if name in catalog:
            result.append(name)
            continue
        plugin = bot.plugins.get(name)
        if plugin is not None:
            result.extend(spec.qualified_name for spec in plugin.ai_tools)
    return list(dict.fromkeys(result))


async def migrate_tool_permissions(bot: FLLBot) -> None:
    """把 0.2.0 之前按插件授权的 ``allowed_plugins`` 迁移为按工具授权。

    迁移是幂等的：已有 ``allowed_tools`` 时不再覆盖，只清理旧键。
    """
    agent = bot.plugins.get("agent")
    if agent is None:
        return
    storage = bot.storage
    scope = agent.settings.scope
    raw = await storage.kv_all(scope)
    migrated = 0
    for compound, value in list(raw.items()):
        group_text, _, key = str(compound).partition(":")
        if key != "allowed_plugins":
            continue
        try:
            group_id = int(group_text)
        except ValueError:
            group_id = 0
        if f"{group_id}:allowed_tools" not in raw:
            tools = expand_tool_names(bot, value)
            await agent.settings.set("allowed_tools", tools, group_id or None)
            migrated += 1
        await storage.kv_delete(scope, str(compound))

    for state in bot.permissions.list_groups():
        group_id = int(state["group_id"])
        config = dict(state.get("config") or {})
        policy = config.get("policy")
        if not isinstance(policy, dict) or "allowed_plugins" not in policy:
            continue
        legacy = policy.get("allowed_plugins")
        updated = dict(policy)
        updated.pop("allowed_plugins", None)
        if legacy is not None and "allowed_tools" not in updated:
            updated["allowed_tools"] = expand_tool_names(bot, legacy)
        config["policy"] = updated
        await bot.permissions.set_group_config(group_id, config)
        migrated += 1
    if migrated:
        logger.info("已把 %d 处按插件授权的工具设置迁移为按工具授权", migrated)


def policy_overrides(bot: FLLBot, group_id: int) -> dict[str, Any]:
    raw = bot.permissions.group_config(group_id).get("policy")
    if not isinstance(raw, dict):
        return {}
    try:
        return GroupPolicy.model_validate(raw).model_dump(exclude_none=True)
    except ValidationError:
        return {}


def agent_settings(bot: FLLBot, group_id: int, self_id: int | None = None) -> dict[str, Any]:
    plugin = bot.plugins.get("agent")
    values = plugin.settings.values() if plugin else {
        "auto_group": False, "trigger_mode": "all", "trigger_prefix": "ai",
        "cooldown_seconds": 5, "max_tool_rounds": 3, "allowed_tools": [],
    }
    defaults = bot.bots.defaults(self_id)
    if defaults.reply_mode != "inherit":
        values["auto_group"] = defaults.reply_mode != "off"
        if defaults.reply_mode != "off":
            values["trigger_mode"] = defaults.reply_mode
    if plugin:
        values.update(plugin.settings.group_overrides(group_id))
    overrides = policy_overrides(bot, group_id)
    for key in values:
        if key in overrides:
            values[key] = overrides[key]
    values["trigger_modes"] = list(dict.fromkeys(overrides.get("trigger_modes") or [values.get("trigger_mode", "all")]))
    values["trigger_mode"] = values["trigger_modes"][0]
    # Legacy group prefixes remain readable; the Bot owns the wake word.
    values["trigger_prefix"] = bot.config.chat.wake_word.strip() or (bot.bots.wake_word(self_id) if self_id else "Bot 唤醒词")
    return values


def effective_policy(bot: FLLBot, group_id: int, self_id: int | None = None) -> dict[str, Any]:
    values = agent_settings(bot, group_id, self_id)
    overrides = policy_overrides(bot, group_id)
    provider = overrides.get("provider") or bot.bots.defaults(self_id).provider or bot.config.llm.route(scene="group", task="agent")
    providers = {item["name"]: item for item in bot.chat.client.providers_status()}
    selected = providers.get(provider)
    catalog = tool_index(bot)
    allowed_tools = [str(name) for name in (values.get("allowed_tools") or [])]
    available_tools = [
        name for name in allowed_tools
        if name in catalog and bot.permissions.plugin_enabled(catalog[name]["plugin"], group_id)
    ]
    reasons = []
    if not bot.permissions.group_enabled(group_id):
        reasons.append("该群总开关或访问范围不允许回复")
    if not bot.plugins.get("agent") or not bot.permissions.plugin_enabled("agent", group_id):
        reasons.append("该群的智能体插件未启用")
    if not bot.chat.scene_enabled("group", group_id):
        reasons.append("模型总开关、群聊 AI 开关或单群 AI 开关未启用")
    if not selected or not selected["configured"]:
        reasons.append("策略选择的模型服务不存在或缺少密钥")
    if not values["auto_group"]:
        reasons.append("策略未启用群聊响应")
    if values["trigger_modes"] == ["prefix"] and not str(values["trigger_prefix"] or "").strip():
        reasons.append("唤醒词尚未设置，请到 Agent 页面填写")
    return {
        **values, "name": (overrides.get("name") or "未命名策略") if overrides else "跟随原有配置",
        "custom": bool(overrides), "provider": provider, "model": selected["model"] if selected else "不可用",
        "persona": bot.chat.persona_name("group", group_id, self_id), "system_extra": overrides.get("system_extra", ""),
        "configured": not reasons, "blocked_by": reasons,
        "available_tools": available_tools,
        "tools": [catalog[name] for name in available_tools],
        "unavailable_tools": [name for name in allowed_tools if name not in available_tools],
    }
