"""控制台任务接口：集中展示就绪状态、会话目录与单群生效配置。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from ..bots import BotDefaults
from ..config import ChatConfig
from ..policy import GroupPolicy, effective_policy
from .auth import build_admin_dependency

if TYPE_CHECKING:
    from ..app import FLLBot


class GroupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    # None resets the group override; the global LLM switch always takes precedence.
    llm_enabled: bool | None = None
    persona: str = ""
    policy: GroupPolicy | None = None


class GroupAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access: Literal["inherit", "allow", "deny"]


class BatchGroupAccess(GroupAccess):
    group_ids: list[int] = Field(min_length=1, max_length=200)


class DefaultGroupAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["all", "listed"]


def create_console_router(bot: FLLBot) -> APIRouter:
    router = APIRouter(prefix="/api/console", dependencies=[Depends(build_admin_dependency(bot))])

    access_lock = asyncio.Lock()

    @router.get("/chat", summary="Agent 聊天信息与唤醒参数")
    async def chat_settings() -> dict[str, Any]:
        return bot.config.chat.model_dump()

    @router.put("/chat", summary="保存 Agent 聊天参数")
    async def save_chat_settings(payload: ChatConfig) -> dict[str, Any]:
        await bot.update_config({"chat": payload.model_dump()})
        return bot.config.chat.model_dump()

    def access_mode() -> str:
        permission = bot.config.permission
        return permission.group_access if permission.group_access != "auto" else "listed" if permission.allow_groups else "all"

    async def update_group_access(group_id: int, access: str) -> None:
        permission = bot.config.permission
        allowed, denied = set(permission.allow_groups), set(permission.deny_groups)
        allowed.discard(group_id)
        denied.discard(group_id)
        if access == "allow":
            allowed.add(group_id)
        elif access == "deny":
            denied.add(group_id)
        # Freeze the effective mode before removing the last allowed group.
        await bot.update_config({"permission": {"group_access": access_mode(), "allow_groups": sorted(allowed), "deny_groups": sorted(denied)}})

    @router.get("/access", summary="配置表默认访问规则")
    async def get_default_access() -> dict[str, Any]:
        return {"mode": access_mode()}

    @router.patch("/access", summary="保存配置表默认访问规则")
    async def save_default_access(payload: DefaultGroupAccess) -> dict[str, Any]:
        async with access_lock:
            await bot.update_config({"permission": {"group_access": payload.mode}})
        return {"ok": True, "mode": access_mode()}

    @router.get("/bots", summary="Bot 账号与默认设置")
    async def list_bots() -> dict[str, Any]:
        return {"items": bot.bots.summaries()}

    @router.post("/bots/refresh", summary="同步 Bot 昵称")
    async def refresh_bots() -> dict[str, Any]:
        connections = [conn for conn in bot.manager.list() if conn.online]
        results = await asyncio.gather(*(bot.bots.sync(conn) for conn in connections), return_exceptions=True)
        return {"items": bot.bots.summaries(), "failed": sum(isinstance(result, Exception) for result in results)}

    @router.put("/bots/{self_id}/defaults", summary="保存这个 Bot 的默认设置")
    async def save_bot_defaults(payload: BotDefaults, self_id: int = Path(gt=0)) -> dict[str, Any]:
        if not any(item["self_id"] == self_id for item in bot.bots.summaries()):
            raise HTTPException(404, "账号尚未连接过，请先连接 Bot")
        if payload.provider and payload.provider not in bot.config.llm.provider_names():
            raise HTTPException(400, "模型服务不存在")
        if payload.persona and bot.personas.find(payload.persona) is None:
            raise HTTPException(400, "人格不存在")
        await bot.bots.update(self_id, defaults=payload.model_dump())
        return {"ok": True, "defaults": payload.model_dump()}

    @router.get("/readiness", summary="机器人就绪检查")
    async def readiness() -> dict[str, Any]:
        agent = bot.plugins.get("agent")
        groups = bot.permissions.list_groups()
        responding = [
            item["group_id"] for item in groups
            if effective_policy(bot, item["group_id"])["configured"]
        ]
        global_response = bool(agent and agent.settings.get("auto_group"))
        return {
            "account_connected": any(item.online for item in bot.manager.list()),
            "model_configured": bot.chat.available,
            "superusers_configured": bool(bot.config.bot.superusers),
            "agent_enabled": bool(agent and bot.permissions.plugin_enabled("agent", None)),
            "global_response": global_response,
            "group_ai_enabled": bot.config.llm.enable_group,
            "responding_groups": responding,
            "failed_plugins": dict(bot.plugins.failed),
        }

    @router.get("/sessions", summary="已保存的 AI 会话目录")
    async def sessions() -> dict[str, Any]:
        return {"items": await bot.storage.chat_rooms()}

    def has_group_override(group_id: int) -> bool:
        config = bot.permissions.group_config(group_id)
        if group_id in bot.config.permission.allow_groups or group_id in bot.config.permission.deny_groups:
            return True
        if config.get("_console_rule") is True:
            return True
        policy = config.get("policy") or {}
        llm = config.get("llm") or {}
        state = next((item for item in bot.permissions.list_groups() if item["group_id"] == group_id), None)
        agent = bot.plugins.get("agent")
        return bool(
            isinstance(policy, dict) and any(value is not None and value != "" for value in policy.values())
            or isinstance(llm, dict) and ("enabled" in llm or llm.get("persona"))
            or state and not config.get("_inherit_enabled") and state["enabled"] != bot.config.permission.default_group_enabled
            or agent and agent.settings.group_overrides(group_id)
        )

    def group_detail(group_id: int, self_id: int | None = None) -> dict[str, Any]:
        if self_id is None:
            primary = bot.manager.primary()
            self_id = primary.self_id if primary else None
        llm = bot.permissions.group_config(group_id).get("llm") or {}
        if not isinstance(llm, dict):
            llm = {}
        state = next((item for item in bot.permissions.list_groups() if item["group_id"] == group_id), None)
        return {
            "group_id": group_id,
            "has_override": has_group_override(group_id),
            "enabled": bool(state["enabled"]) if state and not bot.permissions.group_config(group_id).get("_inherit_enabled") else bot.config.permission.default_group_enabled,
            "effective_enabled": bot.permissions.group_enabled(group_id),
            "denied": group_id in bot.config.permission.deny_groups,
            "allowed": access_mode() == "all" or group_id in bot.config.permission.allow_groups,
            "access": "deny" if group_id in bot.config.permission.deny_groups else "allow" if group_id in bot.config.permission.allow_groups else "inherit",
            "llm_enabled": llm.get("enabled"),
            "effective_llm_enabled": bot.chat.scene_enabled("group", group_id),
            "persona": llm.get("persona") or "",
            "policy": bot.permissions.group_config(group_id).get("policy"),
            "effective_policy": effective_policy(bot, group_id, self_id),
            "self_id": self_id,
            "bot_nickname": bot.bots.nickname(self_id) if self_id else "",
            "inherited_policy": bot.plugins.get("agent").settings.values(group_id) if bot.plugins.get("agent") else {},
        }

    @router.get("/groups", summary="批量读取群配置列表")
    async def list_group_settings(group_ids: list[int] = Query(default=[], max_length=200)) -> dict[str, Any]:
        if any(group_id <= 0 for group_id in group_ids):
            raise HTTPException(400, "群号必须为正数")
        ids = list(dict.fromkeys(group_ids))
        return {"items": [group_detail(group_id) for group_id in ids]}

    @router.get("/groups/{group_id}", summary="单群设置及实际生效状态")
    async def get_group(group_id: int = Path(gt=0), self_id: int | None = None) -> dict[str, Any]:
        return group_detail(group_id, self_id)

    @router.patch("/groups/batch-access", summary="表格内批量修改选中群准入")
    async def save_batch_group_access(payload: BatchGroupAccess) -> dict[str, Any]:
        ids = list(dict.fromkeys(payload.group_ids))
        if any(group_id <= 0 for group_id in ids):
            raise HTTPException(400, "群号必须为正数")
        async with access_lock:
            permission = bot.config.permission
            allowed, denied = set(permission.allow_groups), set(permission.deny_groups)
            allowed.difference_update(ids)
            denied.difference_update(ids)
            if payload.access == "allow":
                allowed.update(ids)
            elif payload.access == "deny":
                denied.update(ids)
            await bot.update_config({"permission": {"group_access": access_mode(), "allow_groups": sorted(allowed), "deny_groups": sorted(denied)}})
        return {"ok": True, "items": [group_detail(group_id) for group_id in ids]}

    @router.patch("/groups/{group_id}/access", summary="表单内修改单群准入")
    async def save_group_access(payload: GroupAccess, group_id: int = Path(gt=0)) -> dict[str, Any]:
        async with access_lock:
            await update_group_access(group_id, payload.access)
            config = bot.permissions.group_config(group_id)
            config["_console_rule"] = True
            await bot.permissions.save_group(group_id, enabled=group_detail(group_id)["enabled"], config=config)
        return {"ok": True, **group_detail(group_id)}

    @router.post("/groups/{group_id}/rule", summary="主动新增单群配置")
    async def add_group_rule(group_id: int = Path(gt=0)) -> dict[str, Any]:
        config = bot.permissions.group_config(group_id)
        config["_console_rule"] = True
        detail = group_detail(group_id)
        await bot.permissions.save_group(group_id, enabled=detail["enabled"], config=config)
        return {"ok": True, **group_detail(group_id)}

    @router.delete("/groups/{group_id}/rule", summary="移除单群配置，恢复默认行为")
    async def remove_group_rule(group_id: int = Path(gt=0)) -> dict[str, Any]:
        config = bot.permissions.group_config(group_id)
        config.pop("policy", None)
        llm = config.get("llm")
        if isinstance(llm, dict):
            llm = {key: value for key, value in llm.items() if key not in {"enabled", "persona"}}
            if llm:
                config["llm"] = llm
            else:
                config.pop("llm", None)
        config["_console_rule"] = False
        config["_inherit_enabled"] = True
        async with access_lock:
            await update_group_access(group_id, "inherit")
            await bot.permissions.save_group(group_id, enabled=bot.config.permission.default_group_enabled, config=config, reset_agent=True)
        return {"ok": True, **group_detail(group_id)}

    @router.patch("/groups/{group_id}", summary="一次保存单群开关与模型覆盖")
    async def save_group(payload: GroupSettings, group_id: int = Path(gt=0)) -> dict[str, Any]:
        if payload.persona and bot.personas.find(payload.persona) is None:
            raise HTTPException(400, "所选人格不存在，请刷新人格列表")
        config = bot.permissions.group_config(group_id)
        config["_console_rule"] = True
        config.pop("_inherit_enabled", None)
        llm = config.get("llm") or {}
        llm = dict(llm) if isinstance(llm, dict) else {}
        for key, value in {"enabled": payload.llm_enabled, "persona": payload.persona or None}.items():
            if value is None:
                llm.pop(key, None)
            else:
                llm[key] = value
        config["llm"] = llm
        if "policy" in payload.model_fields_set:
            if payload.policy is None:
                config.pop("policy", None)
            else:
                policy = payload.policy
                if policy.trigger_modes is not None:
                    policy.trigger_modes = list(dict.fromkeys(policy.trigger_modes))
                    policy.trigger_mode = None
                if policy.provider and policy.provider not in bot.config.llm.provider_names():
                    raise HTTPException(400, "策略选择的模型服务不存在，请刷新模型服务列表")
                if policy.allowed_tools is not None:
                    if any(not name or len(name) > 120 for name in policy.allowed_tools):
                        raise HTTPException(400, "工具名称无效")
                    policy.allowed_tools = list(dict.fromkeys(policy.allowed_tools))
                config["policy"] = policy.model_dump(exclude_none=True)
        await bot.permissions.save_group(group_id, enabled=payload.enabled, config=config)
        return {"ok": True, **group_detail(group_id)}

    return router
