"""管理 REST API：供桌面端 / 自建面板调用。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..llm.client import LLMError
from ..logging_setup import get_ring
from .auth import build_admin_dependency

if TYPE_CHECKING:  # pragma: no cover
    from ..app import FLLBot

logger = logging.getLogger("fllbot.api")


class PluginToggle(BaseModel):
    enabled: bool = True
    group_id: int | None = None


class GroupToggle(BaseModel):
    enabled: bool = True


class LLMTestRequest(BaseModel):
    prompt: str
    persona: str | None = None


class LLMChatRequest(BaseModel):
    message: str
    scene: str = "private"
    scene_id: int = 0
    persona: str | None = None
    include_history: bool = True
    save: bool = False


class ActionRequest(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    self_id: int | None = None


def create_api_router(bot: FLLBot) -> APIRouter:
    router = APIRouter(prefix="/api")
    protected = APIRouter(dependencies=[Depends(build_admin_dependency(bot))])

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

    @protected.get("/connections", summary="NapCat 连接列表")
    async def connections() -> list[dict[str, Any]]:
        return bot.manager.summaries()

    # -- 插件 ------------------------------------------------------------- #
    @protected.get("/plugins", summary="插件列表")
    async def list_plugins() -> list[dict[str, Any]]:
        return bot.plugin_summaries()

    @protected.get("/plugins/{name}", summary="单个插件详情")
    async def plugin_detail(name: str) -> dict[str, Any]:
        for item in bot.plugin_summaries():
            if item["name"] == name:
                return item
        raise HTTPException(404, f"插件不存在: {name}")

    @protected.post("/plugins/reload", summary="重载全部插件或指定插件")
    async def reload_plugins(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        name = (payload or {}).get("name")
        if name:
            try:
                await bot.plugins.reload(str(name))
            except KeyError:
                raise HTTPException(404, f"插件不存在: {name}") from None
            except Exception as exc:
                raise HTTPException(500, f"重载失败: {exc}") from exc
            return {"ok": True, "reloaded": str(name)}
        await bot.plugins.reload_all()
        return {"ok": True, "reloaded": bot.plugins.names()}

    @protected.post("/plugins/{name}/reload", summary="重载指定插件")
    async def reload_plugin(name: str) -> dict[str, Any]:
        try:
            await bot.plugins.reload(name)
        except KeyError:
            raise HTTPException(404, f"插件不存在: {name}") from None
        except Exception as exc:
            raise HTTPException(500, f"重载失败: {exc}") from exc
        return {"ok": True, "reloaded": name}

    @protected.post("/plugins/{name}/enabled", summary="启用/停用插件（可指定群）")
    async def toggle_plugin(name: str, payload: PluginToggle) -> dict[str, Any]:
        if bot.plugins.get(name) is None:
            raise HTTPException(404, f"插件不存在: {name}")
        await bot.permissions.set_plugin_enabled(name, payload.enabled, payload.group_id)
        return {"ok": True, "name": name, "enabled": payload.enabled, "group_id": payload.group_id}

    # -- 群 --------------------------------------------------------------- #
    @protected.get("/groups", summary="已知群列表与开关状态")
    async def list_groups() -> list[dict[str, Any]]:
        return bot.permissions.list_groups()

    @protected.post("/groups/{group_id}/enabled", summary="开关某个群")
    async def toggle_group(group_id: int, payload: GroupToggle) -> dict[str, Any]:
        await bot.permissions.set_group_enabled(group_id, payload.enabled)
        return {"ok": True, "group_id": group_id, "enabled": payload.enabled}

    @protected.get("/groups/{group_id}/config", summary="群配置")
    async def group_config(group_id: int) -> dict[str, Any]:
        return {"group_id": group_id, "config": bot.permissions.group_config(group_id)}

    @protected.patch("/groups/{group_id}/config", summary="更新群配置（与现有配置深合并）")
    async def update_group_config(group_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        current = bot.permissions.group_config(group_id)
        merged = _deep_merge(current, payload)
        await bot.permissions.set_group_config(group_id, merged)
        return {"ok": True, "group_id": group_id, "config": merged}

    # -- 配置 ------------------------------------------------------------- #
    @protected.get("/config", summary="当前生效配置（密钥已隐藏）")
    async def get_config() -> dict[str, Any]:
        return {
            "config": bot.safe_config(),
            "overrides": bot.config_store.overrides,
        }

    @protected.patch("/config", summary="运行时更新配置")
    async def patch_config(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            config = await bot.update_config(payload)
        except Exception as exc:
            raise HTTPException(400, f"配置无效: {exc}") from exc
        return {"ok": True, "config": _masked(config)}

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
    @protected.get("/personas", summary="可用人格")
    async def personas() -> list[dict[str, Any]]:
        return [persona.summary() for persona in bot.personas.list()]

    @protected.get("/personas/{name}", summary="人格详情")
    async def persona_detail(name: str) -> dict[str, Any]:
        persona = bot.personas.find(name)
        if persona is None:
            raise HTTPException(404, f"人格不存在: {name}")
        return {**persona.summary(), "content": persona.content}

    @protected.post("/personas/reload", summary="重新扫描人格目录")
    async def reload_personas() -> dict[str, Any]:
        bot.personas.reload()
        return {"ok": True, "personas": bot.personas.names()}

    @protected.post("/llm/test", summary="直接测试模型连通性")
    async def llm_test(payload: LLMTestRequest) -> dict[str, Any]:
        if not bot.chat.available:
            raise HTTPException(400, "尚未启用 LLM 或未配置 API Key")
        try:
            reply = await bot.chat.test(payload.prompt, persona_name=payload.persona)
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        return reply.summary()

    @protected.post("/llm/chat", summary="按会话上下文测试对话")
    async def llm_chat(payload: LLMChatRequest) -> dict[str, Any]:
        if not bot.chat.available:
            raise HTTPException(400, "尚未启用 LLM 或未配置 API Key")
        scene = payload.scene if payload.scene in {"group", "private"} else "private"
        messages = await bot.chat.build_messages(
            scene,
            payload.scene_id,
            payload.message,
            persona_name=payload.persona,
            include_history=payload.include_history,
        )
        try:
            reply = await bot.chat.complete(messages)
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        if payload.save:
            await bot.storage.add_chat_message(scene, payload.scene_id, "user", payload.message)
            await bot.storage.add_chat_message(scene, payload.scene_id, "assistant", reply.text)
        return {**reply.summary(), "messages": len(messages)}

    # -- 会话历史 --------------------------------------------------------- #
    @protected.get("/chat/{scene}/{scene_id}", summary="会话历史")
    async def chat_history(
        scene: str,
        scene_id: int,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        rows = await bot.storage.recent_chat(scene, scene_id, limit=limit)
        return {"scene": scene, "scene_id": scene_id, "messages": [{"role": r, "content": c} for r, c in rows]}

    @protected.delete("/chat/{scene}/{scene_id}", summary="清空会话历史")
    async def clear_chat(scene: str, scene_id: int) -> dict[str, Any]:
        removed = await bot.storage.clear_chat(scene, scene_id)
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

    @protected.put("/kv/{scope}/{key}", summary="写入 KV")
    async def kv_set(scope: str, key: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await bot.storage.kv_set(scope, key, payload.get("value"))
        return {"ok": True, "scope": scope, "key": key}

    @protected.delete("/kv/{scope}/{key}", summary="删除 KV")
    async def kv_delete(scope: str, key: str) -> dict[str, Any]:
        await bot.storage.kv_delete(scope, key)
        return {"ok": True}

    # -- 实时事件流 ------------------------------------------------------- #
    @router.get("/events/stream", summary="SSE 实时事件流")
    async def event_stream(token: str = Query(default="")) -> StreamingResponse:
        expected = bot.admin_token
        if expected and token != expected:
            raise HTTPException(401, "管理令牌无效")

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
