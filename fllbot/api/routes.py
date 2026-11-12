"""管理 REST API：供桌面端 / 自建面板调用。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from .. import __version__
from ..config import ConfigConflictError, StartupConfigError
from ..conversation import CapacityExceeded, ServiceClosing
from ..llm.client import DEFAULT_CONTEXT_WINDOW, LLMError
from ..logging_setup import get_ring
from ..maintenance import BackupBusyError
from ..policy import tool_index
from ..resources import resource_references
from .auth import build_admin_dependency, build_settings_dependency

if TYPE_CHECKING:  # pragma: no cover
    from ..app import FLLBot

logger = logging.getLogger("fllbot.api")


class PluginToggle(BaseModel):
    enabled: bool = True
    group_id: int | None = Field(default=None, gt=0)


class GroupToggle(BaseModel):
    enabled: bool = True


class LLMTestRequest(BaseModel):
    prompt: str
    persona: str | None = None
    provider: str | None = None


class LLMChatRequest(BaseModel):
    self_id: int = Field(default=0, ge=0)
    message: str
    scene: str = "private"
    scene_id: int = 0
    persona: str | None = None
    include_history: bool = True
    save: bool = False
    provider: str | None = None


class ActionRequest(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    self_id: int | None = None


class ModelDiscoveryRequest(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    provider: str | None = None
    top_level: bool = False


class ModelDiagnosticRequest(BaseModel):
    provider: str | None = None


def create_api_router(bot: FLLBot) -> APIRouter:
    router = APIRouter(prefix="/api")
    tool_grant_lock = asyncio.Lock()
    protected = APIRouter(dependencies=[Depends(build_admin_dependency(bot))])
    settings_write = build_settings_dependency(bot)

    def plugin_settings_revision(plugin, group_id):
        parts = [plugin.settings.revision(group_id)]
        if plugin.name == 'agent' and group_id:
            parts.append(bot.permissions.group_revision(group_id))
        return hashlib.sha256('|'.join(parts).encode()).hexdigest()

    async def group_write(group_id: int, if_match: str | None = Header(default=None)) -> AsyncIterator[None]:
        async with bot.storage.settings_lock:
            if if_match is not None and if_match != bot.permissions.group_revision(group_id):
                raise HTTPException(409, "群设置已修改，请重新读取后保存。当前修改未提交。")
            yield

    # -- 基础 ------------------------------------------------------------- #
    @router.get("/health", summary="健康检查（无需令牌）")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "started": bot.started,
            "connections": bot.manager.count(),
            "uptime_seconds": round(time.time() - bot.started_at, 1) if bot.started_at else 0.0,
        }

    @router.get("/version")
    async def version() -> dict[str, str]:
        return {"name": "fllbot", "version": __version__}

    @protected.get("/status", summary="整体状态")
    async def status() -> dict[str, Any]:
        return await bot.status()

    @protected.post("/storage/backup", summary="创建一致性数据库备份")
    async def create_storage_backup() -> dict[str, Any]:
        try:
            return await bot.backups.create_now()
        except BackupBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(500, f"数据库备份失败：{exc}") from exc

    @protected.get("/connections", summary="NapCat 连接列表")
    async def connections() -> list[dict[str, Any]]:
        return bot.manager.summaries()

    # -- 插件 ------------------------------------------------------------- #
    @protected.get("/plugins", summary="插件列表")
    async def list_plugins() -> list[dict[str, Any]]:
        return bot.plugin_summaries()

    @protected.get("/tools", summary="工具目录")
    async def list_tools() -> dict[str, Any]:
        agent = bot.plugins.get("agent")
        default_tools = {
            str(item) for item in ((agent.settings.get("allowed_tools") if agent else None) or [])
        }
        group_counts: dict[str, int] = {}
        for state in bot.permissions.list_groups():
            policy = (state.get("config") or {}).get("policy")
            names = policy.get("allowed_tools") if isinstance(policy, dict) else None
            for name in names or []:
                group_counts[str(name)] = group_counts.get(str(name), 0) + 1
        items: list[dict[str, Any]] = []
        for name, info in sorted(tool_index(bot).items()):
            entry = bot.plugins.entry_for(info["plugin"])
            plugin = bot.plugins.get(info["plugin"])
            items.append({
                **info,
                "plugin_version": plugin.version if plugin else "",
                "plugin_builtin": bool(entry.builtin) if entry else False,
                "plugin_enabled": bot.permissions.plugin_enabled(info["plugin"], None),
                "globally_allowed": name in default_tools,
                "group_count": group_counts.get(name, 0),
            })
        tool_plugins = {info["plugin"] for info in tool_index(bot).values()}
        return {
            "items": items,
            "providers": [
                item
                for item in bot.plugin_summaries()
                if item["name"] in tool_plugins
            ],
            "tool_count": len(items),
            "plugin_count": len({item["plugin"] for item in items}),
            "globally_allowed_count": sum(1 for item in items if item["globally_allowed"]),
        }

    @protected.get("/sandbox", summary="智能体执行沙盒状态")
    async def sandbox_status(group_id: int | None = Query(default=None, gt=0)) -> dict[str, Any]:
        data = bot.sandbox.status()
        if group_id:
            policy = bot.sandbox.policy(group_id)
            data["group"] = {
                "group_id": group_id,
                "capabilities": {
                    "read": policy.allow_read, "write": policy.allow_write,
                    "network": policy.allow_network, "private_network": policy.allow_private_network,
                },
                "workspace": str(policy.workspace),
            }
        return data

    @protected.patch("/tools/{tool_id}/enabled", summary="按具体工具设置默认授权", dependencies=[Depends(settings_write)])
    async def toggle_tool(tool_id: str, payload: GroupToggle) -> dict[str, Any]:
        if tool_id not in tool_index(bot):
            raise HTTPException(404, "工具不存在或尚未加载")
        agent = bot.plugins.get("agent")
        if agent is None:
            raise HTTPException(409, "Agent 尚未加载")
        async with tool_grant_lock:
            allowed = list(dict.fromkeys(agent.settings.get("allowed_tools") or []))
            if payload.enabled and tool_id not in allowed:
                allowed.append(tool_id)
            elif not payload.enabled:
                allowed = [name for name in allowed if name != tool_id]
            await agent.settings.set("allowed_tools", allowed)
        return {"ok": True, "tool_id": tool_id, "enabled": payload.enabled, "allowed_tools": allowed}

    @protected.get("/plugins/{name}", summary="单个插件详情")
    async def plugin_detail(name: str) -> dict[str, Any]:
        for item in bot.plugin_summaries():
            if item["name"] == name:
                return item
        raise HTTPException(404, f"插件不存在: {name}")

    @protected.get("/plugins/{name}/readme", summary="插件说明（Markdown）")
    async def plugin_readme(name: str) -> dict[str, Any]:
        plugin = bot.plugins.get(name)
        if plugin is None:
            raise HTTPException(404, f"插件不存在: {name}")
        if not plugin.readme.strip():
            raise HTTPException(404, f"插件没有 README: {name}")
        return {"name": name, "format": "markdown", "content": plugin.readme}

    @protected.post("/plugins/reload", summary="重载全部插件或指定插件", dependencies=[Depends(settings_write)])
    async def reload_plugins(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        name = (payload or {}).get("name")
        if name:
            if bot.plugins.get(str(name)) is None:
                raise HTTPException(404, f"插件不存在: {name}")
            await bot.storage.record_management_change('plugin_reload_started', [f'plugin:{name}'])
            try:
                await bot.plugins.reload(str(name))
            except KeyError:
                raise HTTPException(404, f"插件不存在: {name}") from None
            except Exception as exc:
                await bot.storage.record_management_change('plugin_reload_failed', [f'plugin:{name}', 'outcome:failed'])
                raise HTTPException(500, f"重载失败: {exc}") from exc
            await bot.storage.record_management_change('plugin_reloaded', [f'plugin:{name}', 'outcome:success'])
            return {"ok": True, "reloaded": str(name)}
        names = bot.plugins.names()
        await bot.storage.record_management_change('plugin_reload_started', ['scope:all', f'count:{len(names)}'])
        try:
            result = await bot.plugins.reload_all()
        except Exception as exc:
            await bot.storage.record_management_change('plugin_reload_failed', ['scope:all', 'outcome:failed'])
            raise HTTPException(500, "插件批量重载失败；详情见运行日志") from exc
        for plugin_name in result['reloaded']:
            await bot.storage.record_management_change('plugin_reloaded', [f'plugin:{plugin_name}', 'outcome:success'])
        for plugin_name in result['failed']:
            await bot.storage.record_management_change('plugin_reload_failed', [f'plugin:{plugin_name}', 'outcome:failed'])
        return {"ok": not result['failed'], "reloaded": result['reloaded'], "failed": result['failed']}

    @protected.post("/plugins/{name}/reload", summary="重载指定插件", dependencies=[Depends(settings_write)])
    async def reload_plugin(name: str) -> dict[str, Any]:
        if bot.plugins.get(name) is None:
            raise HTTPException(404, f"插件不存在: {name}")
        await bot.storage.record_management_change('plugin_reload_started', [f'plugin:{name}'])
        try:
            await bot.plugins.reload(name)
        except KeyError:
            raise HTTPException(404, f"插件不存在: {name}") from None
        except Exception as exc:
            await bot.storage.record_management_change('plugin_reload_failed', [f'plugin:{name}', 'outcome:failed'])
            raise HTTPException(500, f"重载失败: {exc}") from exc
        await bot.storage.record_management_change('plugin_reloaded', [f'plugin:{name}', 'outcome:success'])
        return {"ok": True, "reloaded": name}

    @protected.post("/plugins/{name}/enabled", summary="启用/停用插件（可指定群）", dependencies=[Depends(settings_write)])
    async def toggle_plugin(name: str, payload: PluginToggle, response: Response,
                            if_match: str | None = Header(default=None)) -> dict[str, Any]:
        if bot.plugins.get(name) is None:
            raise HTTPException(404, f"插件不存在: {name}")
        if if_match is not None and if_match != bot.permissions.plugin_revision(name, payload.group_id):
            raise HTTPException(409, '插件开关已变化，请刷新并核对后再操作；本次未提交')
        await bot.permissions.set_plugin_enabled(name, payload.enabled, payload.group_id)
        status = bot.permissions.plugin_status(name, payload.group_id)
        response.headers['ETag'] = status['revision']
        return {"ok": True, "name": name, "enabled": payload.enabled, "group_id": payload.group_id,
                'effective': status, 'revision': status['revision']}

    @protected.get('/plugins/{name}/enabled', summary='插件开关来源与最终状态')
    async def plugin_state(name: str, response: Response, group_id: int | None = Query(default=None, gt=0)):
        if bot.plugins.get(name) is None:
            raise HTTPException(404, f'插件不存在: {name}')
        status = bot.permissions.plugin_status(name, group_id)
        response.headers['ETag'] = status['revision']
        return status

    @protected.get("/plugins/{name}/settings", summary="插件配置（schema + 当前值）")
    async def get_plugin_settings(name: str, response: Response, group_id: int | None = Query(default=None, gt=0)) -> dict[str, Any]:
        plugin = bot.plugins.get(name)
        if plugin is None:
            raise HTTPException(404, f"插件不存在: {name}")
        revision = plugin_settings_revision(plugin, group_id)
        response.headers['ETag'] = revision
        return {
            "name": name,
            "revision": revision,
            "group_id": group_id,
            "schema": plugin.settings.schema(),
            "values": plugin.settings.values_for_api(group_id),
            "group_policy": bot.permissions.group_config(group_id).get("policy") if name == "agent" and group_id else None,
        }

    @protected.patch("/plugins/{name}/settings", summary="修改插件配置", dependencies=[Depends(settings_write)])
    async def patch_plugin_settings(name: str, response: Response, payload: dict[str, Any] = Body(...),
                                    if_match: str | None = Header(default=None)) -> dict[str, Any]:
        plugin = bot.plugins.get(name)
        if plugin is None:
            raise HTTPException(404, f"插件不存在: {name}")
        group_id = payload.get("group_id")
        try:
            if group_id is not None and (isinstance(group_id, bool) or not isinstance(group_id, (str, int)) or not str(group_id).isdigit()):
                raise ValueError
            group_id = int(group_id) if group_id is not None else None
            if group_id is not None and group_id <= 0:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, '群号必须是正整数') from exc
        if if_match is not None and if_match != plugin_settings_revision(plugin, group_id):
            raise HTTPException(409, '插件配置已变化，当前草稿已保留；请先复制草稿再重新读取并核对')
        values = payload.get("values")
        if not isinstance(values, dict):
            values = {key: value for key, value in payload.items() if key != "group_id"}
        if not values:
            raise HTTPException(400, "缺少要修改的配置项")
        fields = plugin.settings.fields
        values = {key: value for key, value in values.items()
                  if not (key in fields and fields[key].secret and value == '******')}
        try:
            await plugin.settings.update(values, group_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        revision = plugin_settings_revision(plugin, group_id)
        response.headers['ETag'] = revision
        return {
            "ok": True,
            "revision": revision,
            "name": name,
            "group_id": group_id,
            "values": plugin.settings.values_for_api(int(group_id) if group_id is not None else None),
            "effective": plugin.settings.values_for_api(group_id),
        }

    # -- 群 --------------------------------------------------------------- #
    @protected.get("/groups", summary="已知群列表与开关状态")
    async def list_groups() -> list[dict[str, Any]]:
        return bot.permissions.list_groups()

    @protected.get("/groups/discover", summary="自动获取已加入的群，合并多账号与已保存群")
    async def discover_groups() -> dict[str, Any]:
        connections = [connection for connection in bot.manager.list() if connection.online]
        groups: dict[int, dict[str, Any]] = {}
        permission = bot.config.permission
        saved_ids = set(permission.allow_groups) | set(permission.deny_groups)
        saved_ids.update(item["group_id"] for item in bot.permissions.list_groups())
        for group_id in saved_ids:
            groups[group_id] = {
                "group_id": group_id, "group_name": "", "member_count": None,
                "self_ids": [], "live": False,
            }

        async def fetch(connection: Any) -> Any:
            try:
                data = await asyncio.wait_for(connection.call("get_group_list"), timeout=8)
                return (connection.self_id, data if isinstance(data, list) else None)
            except Exception:  # one unavailable account must not hide other accounts' groups
                return (connection.self_id, None)

        results = await asyncio.gather(*(fetch(connection) for connection in connections))
        failed = 0
        for self_id, rows in results:
            if rows is None:
                failed += 1
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    group_id = int(row.get("group_id") or 0)
                except (TypeError, ValueError):
                    continue
                if group_id <= 0:
                    continue
                group = groups.setdefault(group_id, {
                    "group_id": group_id, "group_name": "", "member_count": None,
                    "self_ids": [], "live": True,
                })
                group["live"] = True
                group["group_name"] = str(row.get("group_name") or group["group_name"])
                count = row.get("member_count")
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    group["member_count"] = count
                if self_id and self_id not in group["self_ids"]:
                    group["self_ids"].append(self_id)
        for group in groups.values():
            group["enabled"] = bot.permissions.group_enabled(group["group_id"])
            group["denied"] = group["group_id"] in permission.deny_groups
        return {
            "groups": sorted(groups.values(), key=lambda item: (not item["live"], item["group_name"], item["group_id"])),
            "allow_groups": permission.allow_groups, "deny_groups": permission.deny_groups,
            "online_accounts": len(connections), "failed_accounts": failed,
            "group_access": permission.group_access,
        }

    @protected.post("/groups/{group_id}/enabled", summary="开关某个群", dependencies=[Depends(group_write)])
    async def toggle_group(group_id: int, payload: GroupToggle) -> dict[str, Any]:
        await bot.permissions.set_group_enabled(group_id, payload.enabled)
        return {"ok": True, "group_id": group_id, "enabled": payload.enabled, "revision": bot.permissions.group_revision(group_id)}

    @protected.get("/groups/{group_id}/config", summary="群配置")
    async def group_config(group_id: int) -> dict[str, Any]:
        return {"group_id": group_id, "config": bot.permissions.group_config(group_id), "revision": bot.permissions.group_revision(group_id)}

    @protected.patch("/groups/{group_id}/config", summary="更新群配置（与现有配置深合并）", dependencies=[Depends(group_write)])
    async def update_group_config(group_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        current = bot.permissions.group_config(group_id)
        merged = _deep_merge(current, payload)
        await bot.permissions.set_group_config(group_id, merged)
        return {"ok": True, "group_id": group_id, "config": merged, "revision": bot.permissions.group_revision(group_id)}

    # -- 配置 ------------------------------------------------------------- #
    @protected.get("/config", summary="当前生效配置（密钥已隐藏）")
    async def get_config() -> dict[str, Any]:
        return {
            "config": bot.safe_config(),
            "overrides": _masked(bot.config_store.overrides),
            "revision": bot.config_revision(),
            'startup': {'active': bot.startup_values, 'inactive_overrides': bot.inactive_startup_overrides,
                        'note': '启动参数仅从启动配置文件或命令读取，后台不能在线修改。历史覆盖保留备查，不参与生效。'},
        }

    @protected.patch("/config", summary="运行时更新配置", dependencies=[Depends(settings_write)])
    async def patch_config(payload: dict[str, Any] = Body(...), if_match: str | None = Header(default=None)) -> dict[str, Any]:
        try:
            config = await bot.update_config(payload, expected_revision=if_match)
        except ConfigConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except StartupConfigError as exc:
            raise HTTPException(409, {'code': 'startup_config_read_only', 'fields': exc.fields, 'message': str(exc)}) from exc
        except ValidationError as exc:
            messages = ['.'.join(map(str, error['loc']))+': '+error['msg'] for error in exc.errors(include_input=False, include_context=False)]
            raise HTTPException(400, '配置无效：'+'；'.join(messages)) from exc
        except Exception as exc:
            logger.exception('配置保存失败')
            raise HTTPException(500, '配置保存失败，未发布新配置；请检查运行日志') from exc
        return {"ok": True, "config": _masked(config), "revision": bot.config_revision()}

    # -- 日志与统计 ------------------------------------------------------- #
    @protected.get("/logs", summary="最近的日志")
    async def logs(
        limit: int = Query(default=200, ge=1, le=2000),
        level: str | None = Query(default=None),
        since: str | None = Query(default=None),
    ) -> dict[str, Any]:
        return {"items": get_ring(limit, level=level, since=since)}

    @protected.get("/stats", summary="消息与运行统计")
    async def stats(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
        return {
            "total_messages": await bot.storage.total_messages(),
            "daily": await bot.storage.daily_message_counts(days),
            "top_users": await bot.storage.top_users(10),
            "chat_rooms": await bot.storage.chat_rooms(),
            "dispatcher": bot.dispatcher.summary(),
            "connections": bot.manager.summaries(),
            "llm": bot.chat.status(),
        }

    # -- 人格与 LLM ------------------------------------------------------- #
    @protected.get("/personas", summary="可用人格", dependencies=[Depends(settings_write)])
    async def personas() -> list[dict[str, Any]]:
        bot.personas.reload()
        return [
            {**persona.summary(), "editable": bot.personas.is_editable(persona.name)}
            for persona in bot.personas.list()
        ]

    @protected.get("/personas/variables", summary="人格提示词可用变量")
    async def persona_variables() -> list[dict[str, str]]:
        from ..llm.personas import PERSONA_VARIABLES

        return [dict(item) for item in PERSONA_VARIABLES]

    @protected.get("/personas/{name}", summary="人格详情", dependencies=[Depends(settings_write)])
    async def persona_detail(name: str, response: Response) -> dict[str, Any]:
        bot.personas.reload()
        persona = bot.personas.find(name)
        if persona is None:
            raise HTTPException(404, f"人格不存在: {name}")
        response.headers['ETag'] = persona.revision
        return {**persona.summary(), "content": persona.content, "editable": bot.personas.is_editable(name)}

    def check_persona_revision(name: str, if_match: str | None, if_none_match: str | None = None) -> None:
        if if_match is not None and if_none_match is not None:
            raise HTTPException(400, '不能同时指定 If-Match 和 If-None-Match')
        if if_none_match is not None and if_none_match != '*':
            raise HTTPException(400, '创建人格仅支持 If-None-Match: *')
        bot.personas.reload()
        current = bot.personas.find(name)
        if if_none_match == '*' and current is not None:
            raise HTTPException(409, '同名人格已存在，草稿已保留；请更换标识或重新读取现有人格')
        if if_match is not None and (current is None or current.revision != if_match):
            raise HTTPException(409, '人格已被修改或删除，草稿已保留；请复制草稿后重新读取，再核对并保存')

    @protected.put("/personas/{name}", summary="创建或更新自定义人格", dependencies=[Depends(settings_write)])
    async def save_persona(name: str, response: Response, payload: dict[str, Any] = Body(...),
                           if_match: str | None = Header(default=None),
                           if_none_match: str | None = Header(default=None)) -> dict[str, Any]:
        check_persona_revision(name, if_match, if_none_match)
        operation = 'persona_saved' if bot.personas.find(name) is not None else 'persona_created'
        await bot.storage.record_management_change(f'{operation}_started', [f'persona:{name}'])
        try:
            persona = bot.personas.save(
                name,
                str(payload.get("title", "")),
                str(payload.get("description", "")),
                str(payload.get("content", "")),
            )
        except ValueError as exc:
            await bot.storage.record_management_change(f'{operation}_failed', [f'persona:{name}', 'outcome:failed'])
            raise HTTPException(400, str(exc)) from exc
        except Exception:
            await bot.storage.record_management_change(f'{operation}_failed', [f'persona:{name}', 'outcome:failed'])
            raise
        await bot.storage.record_management_change(operation, [f'persona:{name}', 'outcome:success'])
        response.headers['ETag'] = persona.revision
        return {**persona.summary(), "content": persona.content, "editable": True}

    @protected.delete("/personas/{name}", summary="删除自定义人格", dependencies=[Depends(settings_write)])
    async def delete_persona(name: str, if_match: str | None = Header(default=None)) -> dict[str, Any]:
        check_persona_revision(name, if_match)
        bindings = [item for item in resource_references(bot, "persona", name) if item["scope"] != "system"]
        if bindings:
            raise HTTPException(409, "人格仍被机器人或群配置引用，请先到引用位置更换人格，再删除。")
        was_default = bot.config.llm.persona == name
        await bot.storage.record_management_change('persona_delete_started', [f'persona:{name}', f'default:{str(was_default).lower()}'])
        try:
            if was_default:
                await bot.update_config({"llm": {"persona": ""}})
            bot.personas.delete(name)
        except KeyError as exc:
            await bot.storage.record_management_change('persona_delete_failed', [f'persona:{name}', 'outcome:failed'])
            raise HTTPException(404, f"人格不存在: {name}") from exc
        except ValueError as exc:
            if was_default:
                await bot.update_config({"llm": {"persona": name}})
            await bot.storage.record_management_change('persona_delete_failed', [f'persona:{name}', 'outcome:failed'])
            raise HTTPException(400, str(exc)) from exc
        except Exception:
            # A cleared default is safe if restoring it fails: runtime falls back instead of holding a dangling reference.
            if was_default:
                try:
                    await bot.update_config({"llm": {"persona": name}})
                except Exception:
                    await bot.storage.record_management_change('persona_default_restore_failed', [f'persona:{name}', 'outcome:failed'])
            await bot.storage.record_management_change('persona_delete_failed', [f'persona:{name}', 'outcome:failed'])
            raise
        await bot.storage.record_management_change('persona_deleted', [f'persona:{name}', 'outcome:success', f'default_cleared:{str(was_default).lower()}'])
        return {"ok": True, "personas": bot.personas.names(), "default": bot.config.llm.persona}

    @protected.post("/personas/reload", summary="重新扫描人格目录", dependencies=[Depends(settings_write)])
    async def reload_personas() -> dict[str, Any]:
        await bot.storage.record_management_change('persona_reload_started', ['scope:all'])
        bot.personas.reload()
        await bot.storage.record_management_change('personas_reloaded', ['scope:all', f'count:{len(bot.personas.names())}', 'outcome:success'])
        return {"ok": True, "personas": bot.personas.names()}

    @protected.post("/llm/test", summary="直接测试模型连通性")
    async def llm_test(payload: LLMTestRequest) -> dict[str, Any]:
        if not payload.provider and not bot.chat.available:
            raise HTTPException(400, "尚未启用 LLM 或未配置 API Key")
        try:
            reply = await bot.chat.test(payload.prompt, persona_name=payload.persona, provider=payload.provider)
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        return reply.summary()

    @protected.post("/llm/chat", summary="按会话上下文测试对话")
    async def llm_chat(payload: LLMChatRequest) -> dict[str, Any]:
        if not bot.chat.available:
            raise HTTPException(400, "尚未启用 LLM 或未配置 API Key")
        scene = payload.scene if payload.scene in {"group", "private"} else "private"
        try:
            return await bot.chat.preview(scene, payload.scene_id, payload.message,
                                          persona_name=payload.persona, provider=payload.provider,
                                          include_history=payload.include_history, save=payload.save, self_id=payload.self_id)
        except ServiceClosing as exc:
            raise HTTPException(503, '服务正在关闭') from exc
        except CapacityExceeded as exc:
            raise HTTPException(429, '会话队列已满，请稍后再试') from exc
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @protected.get("/llm/providers", summary="模型提供方状态")
    async def llm_providers() -> list[dict[str, Any]]:
        return bot.chat.client.providers_status()

    @protected.post("/llm/diagnose", summary="验证已保存模型的对话、工具和识图能力")
    async def llm_diagnose(payload: ModelDiagnosticRequest) -> dict[str, Any]:
        if not bot.chat.available:
            raise HTTPException(400, "尚未启用 LLM 或未配置 API Key")
        from ..llm.diagnostics import diagnose
        try:
            return await diagnose(bot.chat, payload.provider)
        except LLMError as exc:
            raise HTTPException(400, str(exc)) from exc

    @protected.get("/llm/models", summary="可选模型列表")
    async def llm_models(provider: str | None = None) -> list[str]:
        try:
            return await bot.chat.client.list_models(provider)
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc

    @protected.post("/llm/models/discover", summary="用当前或未保存的凭据获取模型及上下文窗口")
    async def discover_models(payload: ModelDiscoveryRequest) -> dict[str, Any]:
        try:
            models = await bot.chat.client.model_catalog(
                provider=payload.provider, base_url=payload.base_url, api_key=payload.api_key,
                top_level=payload.top_level,
            )
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"models": models, "fallback_context_window": DEFAULT_CONTEXT_WINDOW}

    @protected.get("/llm/usage", summary="模型用量统计")
    async def llm_usage(range: str = "today") -> dict[str, Any]:
        from datetime import datetime

        now = time.time()
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        since = {
            "today": midnight,
            "7d": now - 7 * 86400,
            "30d": now - 30 * 86400,
            "all": 0.0,
        }.get(range, midnight)
        summary = await bot.storage.llm_usage_summary(since)
        return {"range": range, "since": since, **summary}

    # -- 会话历史 --------------------------------------------------------- #
    @protected.get("/chat/{scene}/{scene_id}", summary="会话历史")
    async def chat_history(
        scene: str,
        scene_id: int,
        limit: int = Query(default=50, ge=1, le=500),
        self_id: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        rows = await bot.storage.recent_chat(scene, scene_id, limit=limit, self_id=self_id)
        source = 'chat'
        if scene == 'group' and self_id:
            from types import SimpleNamespace
            memory = await bot.storage.memory.recent(self_id,scene_id,2**63-1,limit=limit)
            if memory:
                rows = [SimpleNamespace(**row) for row in memory]
                source = 'memory'

        return {
            "self_id": self_id,
            "source": source,
            "scene": scene,
            "scene_id": scene_id,
            "messages": [
                {
                    "role": row.role,
                    "content": row.content,
                    "images": json.loads(row.images) if isinstance(getattr(row, 'images', None), str) else getattr(row, 'images', []),
                    "user_id": row.user_id,
                    "nickname": row.nickname,
                    "sender_role": row.sender_role,
                }
                for row in rows
            ],
        }

    @protected.delete("/chat/{scene}/{scene_id}", summary="清空会话历史", dependencies=[Depends(settings_write)])
    async def clear_chat(scene: str, scene_id: int, self_id: int = Query(default=0, ge=0)) -> dict[str, Any]:
        if scene not in {"group", "private"} or scene_id <= 0:
            raise HTTPException(400, "会话场景或编号无效")
        removed = await bot.storage.clear_chat(scene, scene_id, self_id=self_id)
        return {"ok": True, "removed": removed}

    # -- 通用 OneBot 调用 ------------------------------------------------- #
    @protected.post("/actions", summary="代发 OneBot API 请求（调试用）")
    async def call_action(payload: ActionRequest) -> dict[str, Any]:
        connection = bot.manager.by_self_id(payload.self_id)
        if connection is None:
            raise HTTPException(503, "当前没有在线的 NapCat 连接")
        try:
            data = await connection.call(payload.action, payload.params)
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"ok": True, "action": payload.action, "data": data}

    # -- KV（插件配置） --------------------------------------------------- #
    @protected.get("/kv", summary="所有 KV scope")
    async def kv_scopes() -> list[str]:
        return await bot.storage.kv_scopes()

    @protected.get("/kv/{scope}", summary="某个 scope 的全部键值")
    async def kv_all(scope: str) -> dict[str, Any]:
        return await bot.storage.kv_all(scope)

    @protected.put("/kv/{scope}/{key}", summary="写入 KV", dependencies=[Depends(settings_write)])
    async def kv_set(scope: str, key: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await bot.storage.admin_kv_set(scope, key, payload.get("value"))
        return {"ok": True, "scope": scope, "key": key}

    @protected.delete("/kv/{scope}/{key}", summary="删除 KV", dependencies=[Depends(settings_write)])
    async def kv_delete(scope: str, key: str) -> dict[str, Any]:
        deleted = await bot.storage.admin_kv_delete(scope, key)
        return {"ok": True, "deleted": deleted}

    # -- 实时事件流 ------------------------------------------------------- #
    @protected.get("/events/stream", summary="SSE 实时事件流")
    async def event_stream() -> StreamingResponse:
        async def generator():
            async with bot.hub.subscribe() as queue:
                yield ": connected\n\n"
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    router.include_router(protected)
    return router


def _masked(config: Any) -> dict[str, Any]:
    from ..config import mask_secrets

    return mask_secrets(config)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    from ..config import deep_merge

    return deep_merge(base, patch)


__all__ = ["create_api_router"]
