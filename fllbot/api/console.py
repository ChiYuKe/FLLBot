"""控制台任务接口：集中展示就绪状态、会话目录与单群生效配置。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ..bots import BotDefaults
from ..config import ChatConfig, RetentionConfig
from ..conversation import CapacityExceeded, ServiceClosing
from ..maintenance import BackupBusyError
from ..onboarding import setup_guide
from ..policy import GroupPolicy, effective_policy
from ..resources import resource_references
from ..retention import CleanupRequest
from ..services import ServiceCheckBusyError
from .auth import build_admin_dependency, build_settings_dependency

if TYPE_CHECKING:
    from ..app import FLLBot


class AccountState(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


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
    revisions: dict[int, str] | None = None


class DefaultGroupAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["all", "listed"]


class ServiceCheckRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    service_id: str | None = Field(default=None, max_length=160)


class CleanupConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    confirm: Literal[True]


class TaskAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['pause', 'resume', 'cancel', 'complete', 'retry']
    revision: str = Field(min_length=1, max_length=100)
    acknowledge_duplicate: bool = False


class ReplacementTarget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: str = Field(min_length=1, max_length=80)


def create_console_router(bot: FLLBot) -> APIRouter:
    router = APIRouter(prefix="/api/console", dependencies=[Depends(build_admin_dependency(bot))])

    access_lock = bot.storage.settings_lock
    settings_write = build_settings_dependency(bot)

    def task_sources():
        return {f'{plugin.name}__{source.name}': (plugin, source)
                for plugin in bot.plugins.loaded() for source in plugin.task_sources}

    async def task_call(source_id: str, operation: str, **values):
        pair = task_sources().get(source_id)
        if pair is None:
            raise HTTPException(404, '任务来源未加载，请检查插件状态')
        try:
            return await pair[1].callback(operation, **values)
        except KeyError as exc:
            raise HTTPException(404, '任务不存在') from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get('/task-sources', summary='插件声明的任务来源')
    async def list_task_sources():
        return {'items': [{'id': key, 'plugin': plugin.name, 'title': source.title,
                           'enabled': bot.permissions.plugin_enabled(plugin.name, None)}
                          for key, (plugin, source) in task_sources().items()]}

    @router.get('/tasks/{source_id}', summary='按来源筛选持久化任务')
    async def list_tasks(source_id: str, before: int | None = Query(default=None, gt=0),
                         limit: int = Query(default=20, ge=1, le=100),
                         self_id: int | None = Query(default=None, gt=0),
                         scene: Literal['group', 'private'] | None = None,
                         scene_id: int | None = Query(default=None, gt=0),
                         owner: int | None = Query(default=None, gt=0),
                         state: Literal['pending', 'paused', 'completed', 'cancelled'] | None = None):
        if scene_id is not None and scene is None:
            raise HTTPException(422, '指定会话号时请选择场景')
        return await task_call(source_id, 'list', before=before, limit=limit, self_id=self_id,
                               scene=scene, scene_id=scene_id, owner=owner, state=state)

    @router.get('/tasks/{source_id}/{task_id}/runs', summary='持久化任务运行记录')
    async def task_runs(source_id: str, task_id: int = Path(gt=0),
                        before: int | None = Query(default=None, gt=0), limit: int = Query(default=20, ge=1, le=100)):
        return await task_call(source_id, 'runs', task_id=task_id, before=before, limit=limit)

    @router.post('/tasks/{source_id}/{task_id}/actions', summary='按版本暂停、取消或受控重试任务')
    async def task_action(source_id: str, payload: TaskAction, task_id: int = Path(gt=0)):
        return await task_call(source_id, 'action', task_id=task_id, **payload.model_dump())

    def assert_revision(group_id: int, revision: str | None) -> None:
        if revision is not None and revision != bot.permissions.group_revision(group_id):
            raise HTTPException(409, "这个群的设置已被其他窗口或接口修改，请重新读取后再保存。当前修改未提交。")

    async def group_write(group_id: int = Path(gt=0), if_match: str | None = Header(default=None)) -> AsyncIterator[None]:
        # Hold the same lock through validation and mutation, including access changes.
        async with access_lock:
            assert_revision(group_id, if_match)
            yield

    @router.get('/message-events', summary='接收和排队事实（不含正文）')
    async def message_events(self_id: int | None = None, scene_id: int | None = None,
                             before: int | None = Query(default=None, gt=0),
                             limit: int = Query(default=50, ge=1, le=200)) -> dict:
        return await bot.storage.list_message_events(self_id=self_id,scene_id=scene_id,before=before,limit=limit)

    @router.get('/trigger-decisions', summary='触发决策原因（不含正文）')
    async def trigger_decisions(self_id: int | None = None, scene_id: int | None = None,
                                 before: int | None = Query(default=None, gt=0),
                                 limit: int = Query(default=50, ge=1, le=200)) -> dict:
        return await bot.storage.list_trigger_decisions(self_id=self_id,scene_id=scene_id,before=before,limit=limit)

    @router.get('/turns', summary='查询聊天轮次状态（仅元数据）')
    async def list_chat_turns(self_id: int | None = None, scene: Literal['group','private'] | None = None,
                              scene_id: int | None = None, status: str | None = Query(default=None, max_length=40),
                              before: int | None = Query(default=None, gt=0),
                              limit: int = Query(default=50, ge=1, le=200)) -> dict:
        return await bot.storage.list_turns(self_id=self_id, scene=scene, scene_id=scene_id,
                                            status=status, before=before, limit=limit)

    @router.get('/turns/{turn_id}', summary='轮次的工具和分段回执（不返回正文）')
    async def chat_turn_detail(turn_id: str = Path(pattern=r'^[a-f0-9]{32}$')) -> dict:
        detail = await bot.storage.turn_detail(turn_id)
        if detail is None:
            raise HTTPException(404, '轮次不存在')
        return {**detail, 'can_cancel':bot.conversations.can_cancel(turn_id),
                'can_retry':await bot.storage.recovery_plan(turn_id) is not None}

    @router.post('/turns/{turn_id}/recover-delivery', summary='仅恢复明确未发送的分段')
    async def recover_chat_delivery(turn_id: str = Path(pattern=r'^[a-f0-9]{32}$')) -> dict:
        try:
            bot.capacity.ensure_open()
            await bot.storage.record_management_change('delivery_recovery_requested', [f'turn:{turn_id}'])
            return await bot.deliveries.recover(turn_id)
        except ServiceClosing as exc:
            raise HTTPException(503, '服务正在关闭') from exc
        except CapacityExceeded as exc:
            raise HTTPException(429, '会话队列已满，请稍后再试') from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, '分段恢复未完成，请读取轮次状态') from exc

    @router.post('/turns/{turn_id}/cancel', summary='取消进行中的轮次，不撤回已执行动作')
    async def cancel_chat_turn(turn_id: str = Path(pattern=r'^[a-f0-9]{32}$')) -> dict:
        if await bot.storage.turn_detail(turn_id) is None:
            raise HTTPException(404, '轮次不存在')
        await bot.storage.record_management_change('turn_cancel_requested', [f'turn:{turn_id}'])
        if not bot.conversations.cancel(turn_id):
            raise HTTPException(409, '轮次已结束或不在当前进程运行，不能重新执行')
        return {'accepted':True, 'turn_id':turn_id}

    @router.get("/chat", summary="Agent 聊天信息与唤醒参数")
    async def chat_settings(response: Response) -> dict[str, Any]:
        response.headers['ETag'] = bot.config_revision()
        return bot.config.chat.model_dump()

    @router.put("/chat", summary="保存 Agent 聊天参数", dependencies=[Depends(settings_write)])
    async def save_chat_settings(payload: ChatConfig, response: Response, if_match: str | None = Header(default=None)) -> dict[str, Any]:
        if if_match is not None and if_match != bot.config_revision():
            raise HTTPException(409, "聊天配置已被修改，请重新读取后保存。当前修改未提交。")
        await bot.update_config({"chat": payload.model_dump()})
        response.headers['ETag'] = bot.config_revision()
        return bot.config.chat.model_dump()

    def access_mode() -> str:
        permission = bot.config.permission
        return permission.group_access if permission.group_access != "auto" else "listed" if permission.allow_groups else "all"

    def access_revision() -> str:
        return hashlib.sha256(json.dumps(bot.config.permission.model_dump(), sort_keys=True).encode()).hexdigest()

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
        return {"mode": access_mode(), "revision": access_revision()}

    @router.patch("/access", summary="保存配置表默认访问规则")
    async def save_default_access(payload: DefaultGroupAccess, if_match: str | None = Header(default=None)) -> dict[str, Any]:
        async with access_lock:
            if if_match is not None and if_match != access_revision():
                raise HTTPException(409, "访问规则已被修改，请刷新后重试。当前修改未提交。")
            await bot.update_config({"permission": {"group_access": payload.mode}})
            return {"ok": True, "mode": access_mode(), "revision": access_revision()}

    @router.get("/bots", summary="Bot 账号与默认设置")
    async def list_bots() -> dict[str, Any]:
        return {"items": bot.bots.summaries()}

    @router.post("/bots/refresh", summary="同步 Bot 昵称")
    async def refresh_bots() -> dict[str, Any]:
        connections = [conn for conn in bot.manager.list() if conn.online]
        results = await asyncio.gather(*(bot.bots.sync(conn) for conn in connections), return_exceptions=True)
        return {"items": bot.bots.summaries(), "failed": sum(isinstance(result, Exception) for result in results)}

    @router.get('/bots/{self_id}/defaults', summary='读取账号默认设置及可选资源')
    async def bot_defaults(response: Response, self_id: int = Path(gt=0)):
        bot.personas.reload()
        account = next((item for item in bot.bots.summaries() if item['self_id'] == self_id), None)
        if account is None:
            raise HTTPException(404, '账号尚未连接过，请先连接 Bot')
        response.headers['ETag'] = account['revision']
        return {**account, 'providers': bot.config.llm.provider_names(),
                'personas': [{'name': persona.name, 'title': persona.title or persona.name} for persona in bot.personas.list()],
                'global_wake_word': bot.config.chat.wake_word,
                'note': '仅影响此 Bot；群单独配置仍优先。全局唤醒词非空时优先于 Bot 唤醒词。继承和关闭 AI 均不修改群准入、插件及模型硬开关。'}

    @router.put("/bots/{self_id}/defaults", summary="保存这个 Bot 的默认设置", dependencies=[Depends(settings_write)])
    async def save_bot_defaults(payload: BotDefaults, response: Response, self_id: int = Path(gt=0),
                                if_match: str | None = Header(default=None)) -> dict[str, Any]:
        if not any(item["self_id"] == self_id for item in bot.bots.summaries()):
            raise HTTPException(404, "账号尚未连接过，请先连接 Bot")
        if if_match is not None and if_match != bot.bots.revision(self_id):
            raise HTTPException(409, '账号默认设置已变化，草稿已保留；请先复制草稿再重新读取并核对')
        if payload.provider and payload.provider not in bot.config.llm.provider_names():
            raise HTTPException(400, "模型服务不存在")
        bot.personas.reload()
        if payload.persona and bot.personas.find(payload.persona) is None:
            raise HTTPException(400, "人格不存在")
        await bot.bots.update(self_id, defaults=payload.model_dump())
        revision = bot.bots.revision(self_id)
        response.headers['ETag'] = revision
        return {"ok": True, "defaults": payload.model_dump(), 'revision': revision}

    @router.put('/bots/{self_id}/state', summary='启用或停用账号聊天业务', dependencies=[Depends(settings_write)])
    async def account_state(payload: AccountState, self_id: int = Path(gt=0),
                             if_match: str | None = Header(default=None)) -> dict:
        if str(self_id) not in bot.bots.items and bot.manager.by_self_id(self_id) is None:
            raise HTTPException(404, '账号不存在')
        if if_match is not None and if_match != bot.bots.revision(self_id):
            raise HTTPException(409, '账号设置已变化，请重新读取')
        await bot.bots.update(self_id, enabled=payload.enabled)
        return {'self_id':self_id, 'enabled':bot.bots.enabled(self_id), 'revision':bot.bots.revision(self_id)}

    @router.get("/readiness", summary="机器人就绪检查")
    async def readiness() -> dict[str, Any]:
        agent = bot.plugins.get("agent")
        groups = bot.permissions.list_groups()
        accounts = bot.bots.summaries()
        contexts = [item["self_id"] for item in accounts] or [None]
        policies = {
            item["group_id"]: [effective_policy(bot, item["group_id"], self_id) for self_id in contexts]
            for item in groups
        }
        responding = [gid for gid, items in policies.items() if any(item["configured"] for item in items)]
        ready_groups = [gid for gid, items in policies.items() if any(item["diagnostics"]["ready"] for item in items)]
        global_response = bool(agent and agent.settings.get("auto_group"))
        return {
            "account_connected": any(item.online for item in bot.manager.list()),
            "model_configured": bot.chat.available,
            "superusers_configured": bool(bot.config.bot.superusers),
            "agent_enabled": bool(agent and bot.permissions.plugin_enabled("agent", None)),
            "global_response": global_response,
            "group_ai_enabled": bot.config.llm.enable_group,
            "responding_groups": responding,
            "ready_groups": ready_groups,
            "diagnostic_note": "按已知机器人默认值检查已登记群；账号在线与配置就绪不代表已验证模型调用或群成员关系。",
            "failed_plugins": dict(bot.plugins.failed),
        }

    @router.get("/setup", summary="首次接入引导（只检查配置，不发送消息）")
    async def setup(self_id: int | None = Query(default=None, gt=0), group_id: int | None = Query(default=None, gt=0)) -> dict[str, Any]:
        return setup_guide(bot, self_id, group_id)

    @router.get("/sessions", summary="已保存的 AI 会话目录")
    async def sessions() -> dict[str, Any]:
        return {"items": await bot.storage.chat_rooms()}

    @router.get('/services', summary='插件外部服务状态（只读取缓存）')
    async def services() -> dict[str, Any]:
        return bot.services.snapshot()

    @router.get('/data-retention', summary='数据保留策略与清理状态')
    async def data_retention() -> dict[str, Any]:
        return {'policy': bot.config.retention.model_dump(), 'revision': bot.config_revision(),
                'active': bot.cleanup.active, 'last_automatic': bot.cleanup.last_automatic,
                'note': '0 天表示不自动清理；消息诊断仍按固定 7 天/10000 条保留。配置、插件 KV、任务和备份文件不在清理范围。'}

    @router.put('/data-retention', dependencies=[Depends(settings_write)], summary='保存自动保留策略')
    async def save_data_retention(payload: RetentionConfig, if_match: str | None = Header(default=None)) -> dict[str, Any]:
        if if_match is not None and if_match != bot.config_revision():
            raise HTTPException(409, '配置已修改，请重新读取保留策略；本次未保存')
        await bot.update_config({'retention': payload.model_dump()})
        return {'policy': bot.config.retention.model_dump(), 'revision': bot.config_revision()}

    @router.post('/data-cleanup/preview', summary='只读预览待清理记录数量')
    async def cleanup_preview(payload: CleanupRequest) -> dict[str, Any]:
        return await bot.cleanup.preview(payload)

    @router.post('/data-cleanup', status_code=202, summary='确认预览并提交清理作业')
    async def start_cleanup(payload: CleanupConfirmation) -> dict[str, Any]:
        try:
            return bot.cleanup.start(payload.preview_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get('/data-cleanup/jobs/{job_id}', summary='查询清理作业')
    async def cleanup_job(job_id: str = Path(pattern=r'^[a-f0-9]{32}$')) -> dict[str, Any]:
        job = bot.cleanup.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, '清理作业不在本次运行的近期记录中，请核对持久化清理记录')
        return dict(job)

    @router.get('/data-cleanup/runs', summary='查询持久化清理结果')
    async def cleanup_runs(before: int | None = Query(default=None, gt=0), limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return await bot.cleanup.runs(before, limit)

    @router.post('/services/check', summary='按需执行只读服务检查')
    async def check_services(payload: ServiceCheckRequest | None = None) -> dict[str, Any]:
        try:
            return await bot.services.check(payload.service_id if payload else None)
        except KeyError as exc:
            raise HTTPException(404, '服务检查不存在或插件已卸载') from exc
        except ServiceCheckBusyError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get('/backups', summary='数据库备份列表')
    async def backups(before: str | None = None, limit: int = Query(default=30, ge=1, le=100)) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(bot.backups.index, before=before, limit=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post('/backups', status_code=202, summary='创建数据库备份作业')
    async def start_backup() -> dict[str, Any]:
        try:
            return bot.backups.start()
        except BackupBusyError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get('/backup-jobs/{job_id}', summary='查询本次运行的备份作业')
    async def backup_job(job_id: str = Path(pattern=r'^[a-f0-9]{32}$')) -> dict[str, Any]:
        job = bot.backups.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, '作业不存在或已不在本次运行的近期记录中，请刷新备份列表')
        return dict(job)

    @router.post('/backups/{name}/validate', summary='校验数据库备份完整性')
    async def validate_backup(name: str) -> dict[str, Any]:
        try:
            return await bot.backups.validate(name)
        except FileNotFoundError as exc:
            raise HTTPException(404, '备份文件不存在') from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(400, '备份校验失败：文件损坏、版本不支持或路径不被允许') from exc

    @router.get('/backups/{name}/download', summary='下载数据库备份')
    async def download_backup(name: str) -> FileResponse:
        try:
            path = bot.backups.file(name)
        except FileNotFoundError as exc:
            raise HTTPException(404, '备份文件不存在') from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path, filename=name, media_type='application/octet-stream', headers={'Cache-Control': 'no-store'})

    @router.get('/backups/{name}/restore-guide', summary='获取停机恢复指引，不执行恢复')
    async def restore_guide(name: str) -> dict[str, Any]:
        try:
            return bot.backups.restore_guide(name)
        except FileNotFoundError as exc:
            raise HTTPException(404, '备份文件不存在') from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get('/traces', summary='消息处理诊断记录（不包含正文）')
    async def traces(
        self_id: int | None = Query(default=None, gt=0), scene: Literal['group', 'private'] | None = None,
        scene_id: int | None = Query(default=None, gt=0), message_id: int | None = Query(default=None, gt=0),
        outcome: Literal['sent', 'warning', 'failed', 'skipped', 'processed', 'cancelled', 'dropped'] | None = None,
        before: int | None = Query(default=None, gt=0), limit: int = Query(default=30, ge=1, le=100),
    ) -> dict[str, Any]:
        result = await bot.storage.message_traces(self_id=self_id, scene=scene, scene_id=scene_id, message_id=message_id,
                                                  outcome=outcome, before=before, limit=limit)
        return {**result, 'recorder': bot.traces.status(), 'retention_days': 7, 'max_records': 10000}

    @router.get('/traces/{trace_id}', summary='一次消息处理的阶段与原因码')
    async def trace_detail(trace_id: str = Path(pattern=r'^[a-f0-9]{32}$')) -> dict[str, Any]:
        item = await bot.storage.message_trace(trace_id)
        if item is None:
            raise HTTPException(404, '诊断记录不存在或已过保留期')
        return item

    @router.get("/resources/{kind}/{name}/references", summary="共享资源的显式引用位置")
    async def references(kind: Literal["persona", "provider"], name: str) -> dict[str, Any]:
        exists = bot.personas.find(name) is not None if kind == "persona" else name in bot.config.llm.provider_names()
        if not exists:
            raise HTTPException(404, "资源不存在")
        items = resource_references(bot, kind, name)
        alternatives = [{'name': item.name, 'title': item.title or item.name} for item in bot.personas.list() if item.name != name] if kind == 'persona' else [
            {'name': item, 'title': item} for item in bot.config.llm.provider_names() if item != name]
        return {"kind": kind, "name": name, "items": items, "count": len(items), 'alternatives': alternatives}

    @router.post('/resources/{kind}/{name}/replacement-preview', summary='预览直接引用整体替换')
    async def replacement_preview(kind: Literal['persona', 'provider'], name: str, payload: ReplacementTarget):
        try:
            return await bot.replacements.preview(kind, name, payload.target)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post('/resources/replacements', summary='确认预览后原子替换共享资源引用')
    async def replace_references(payload: CleanupConfirmation):
        try:
            return await bot.replacements.apply(payload.preview_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

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
            accounts = bot.bots.summaries()
            self_id = primary.self_id if primary else accounts[0]["self_id"] if accounts else None
        llm = bot.permissions.group_config(group_id).get("llm") or {}
        if not isinstance(llm, dict):
            llm = {}
        state = next((item for item in bot.permissions.list_groups() if item["group_id"] == group_id), None)
        policy = effective_policy(bot, group_id, self_id)
        responding = bot.permissions.group_enabled(group_id) and bot.chat.scene_enabled("group", group_id) and policy["auto_group"]
        attention = bool(responding and (not policy["diagnostics"]["ready"] or policy["unavailable_tools"]))
        return {
            "group_id": group_id,
            "revision": bot.permissions.group_revision(group_id),
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
            "effective_policy": policy,
            "needs_attention": attention,
            "diagnostic_status": "attention" if attention else "ready" if responding else "disabled",
            "self_id": self_id,
            "bot_nickname": bot.bots.nickname(self_id) if self_id else "",
            "inherited_policy": bot.plugins.get("agent").settings.values(group_id) if bot.plugins.get("agent") else {},
        }

    @router.get("/groups", summary="批量读取群配置列表")
    async def list_group_settings(
        group_ids: list[int] = Query(default=[], max_length=200), self_id: int | None = Query(default=None, gt=0),
    ) -> dict[str, Any]:
        if any(group_id <= 0 for group_id in group_ids):
            raise HTTPException(400, "群号必须为正数")
        ids = list(dict.fromkeys(group_ids))
        return {"items": [group_detail(group_id, self_id) for group_id in ids]}

    @router.get("/groups/{group_id}", summary="单群设置及实际生效状态")
    async def get_group(group_id: int = Path(gt=0), self_id: int | None = Query(default=None, gt=0)) -> dict[str, Any]:
        return group_detail(group_id, self_id)

    @router.get("/groups/{group_id}/changes", summary="群设置变更记录（不包含配置内容）")
    async def group_changes(
        group_id: int = Path(gt=0), before: int | None = Query(default=None, gt=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        return await bot.storage.group_changes(group_id, before=before, limit=limit)

    @router.patch("/groups/batch-access", summary="表格内批量修改选中群准入")
    async def save_batch_group_access(payload: BatchGroupAccess, self_id: int | None = Query(default=None, gt=0)) -> dict[str, Any]:
        ids = list(dict.fromkeys(payload.group_ids))
        if any(group_id <= 0 for group_id in ids):
            raise HTTPException(400, "群号必须为正数")
        async with access_lock:
            if payload.revisions is not None:
                if set(payload.revisions) != set(ids):
                    raise HTTPException(400, "批量保存必须提供每个所选群的版本，请刷新后重试")
                for group_id in ids:
                    assert_revision(group_id, payload.revisions[group_id])
            permission = bot.config.permission
            allowed, denied = set(permission.allow_groups), set(permission.deny_groups)
            allowed.difference_update(ids)
            denied.difference_update(ids)
            if payload.access == "allow":
                allowed.update(ids)
            elif payload.access == "deny":
                denied.update(ids)
            await bot.update_config({"permission": {"group_access": access_mode(), "allow_groups": sorted(allowed), "deny_groups": sorted(denied)}})
        return {"ok": True, "items": [group_detail(group_id, self_id) for group_id in ids]}

    @router.patch("/groups/{group_id}/access", summary="表单内修改单群准入", dependencies=[Depends(group_write)])
    async def save_group_access(payload: GroupAccess, group_id: int = Path(gt=0), self_id: int | None = Query(default=None, gt=0)) -> dict[str, Any]:
        await update_group_access(group_id, payload.access)
        config = bot.permissions.group_config(group_id)
        config["_console_rule"] = True
        await bot.permissions.save_group(group_id, enabled=group_detail(group_id)["enabled"], config=config)
        return {"ok": True, **group_detail(group_id, self_id)}

    @router.post("/groups/{group_id}/rule", summary="主动新增单群配置", dependencies=[Depends(group_write)])
    async def add_group_rule(group_id: int = Path(gt=0), self_id: int | None = Query(default=None, gt=0)) -> dict[str, Any]:
        config = bot.permissions.group_config(group_id)
        config["_console_rule"] = True
        config.pop("_inherit_enabled", None)
        await bot.permissions.save_group(group_id, enabled=True, config=config)
        return {"ok": True, **group_detail(group_id, self_id)}

    @router.delete("/groups/{group_id}/rule", summary="移除单群配置，恢复默认行为", dependencies=[Depends(group_write)])
    async def remove_group_rule(group_id: int = Path(gt=0), self_id: int | None = Query(default=None, gt=0)) -> dict[str, Any]:
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
        await update_group_access(group_id, "inherit")
        await bot.permissions.save_group(group_id, enabled=bot.config.permission.default_group_enabled, config=config, reset_agent=True)
        return {"ok": True, **group_detail(group_id, self_id)}

    @router.patch("/groups/{group_id}", summary="一次保存单群开关与模型覆盖", dependencies=[Depends(group_write)])
    async def save_group(payload: GroupSettings, group_id: int = Path(gt=0), self_id: int | None = Query(default=None, gt=0)) -> dict[str, Any]:
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
                data = policy.model_dump(exclude_none=True)
                # 旧版页面只提交自己认识的字段；未提交的群覆盖（例如沙盒开关）应保留，
                # 而显式提交 null 的字段仍表示恢复继承。
                for key, value in (config.get("policy") or {}).items():
                    if key not in policy.model_fields_set:
                        data[key] = value
                config["policy"] = data
        await bot.permissions.save_group(group_id, enabled=payload.enabled, config=config)
        return {"ok": True, **group_detail(group_id, self_id)}

    return router
