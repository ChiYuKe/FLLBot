"""Shared resource references; report explicit bindings without expanding inheritance."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from .bots import BotDefaults
from .storage import resources as resource_store

if TYPE_CHECKING:
    from .app import FLLBot


def resource_references(bot: FLLBot, kind: Literal["persona", "provider"], name: str) -> list[dict[str, Any]]:
    references = []

    def add(scope: str, object_id: int | None, field: str, label: str, target: str) -> None:
        references.append({"scope": scope, "object_id": object_id, "field": field, "label": label, "target": target})

    config = bot.config.llm
    if kind == "persona":
        if config.persona == name:
            add("system", None, "llm.persona", "系统默认人格", "personas")
    else:
        if config.default == name:
            add("system", None, "llm.default", "系统默认模型", "models")
        for route, provider in sorted(config.routes.items()):
            if provider == name:
                add("system", None, f"llm.routes.{route}", f"模型路由：{route}", "models")
        if name in config.fallback_providers:
            add("system", None, "llm.fallback_providers", "备用模型", "models")
    for account in bot.bots.summaries():
        if account["defaults"].get(kind) == name:
            add("bot", account["self_id"], kind, f"机器人 {account['nickname'] or account['self_id']} 默认设置", "accounts")
    for group in bot.permissions.list_groups():
        values = group["config"].get("llm" if kind == "persona" else "policy") or {}
        if isinstance(values, dict) and values.get(kind) == name:
            add("group", group["group_id"], f"{'llm' if kind == 'persona' else 'policy'}.{kind}", f"群 {group['group_id']} 单独配置", "groups")
    return references


class ResourceReplacementManager:
    """Prepare, recheck, persist and publish shared-resource binding changes."""

    MAX_REFERENCES = 1000

    def __init__(self, bot: FLLBot) -> None:
        self.bot = bot
        self.previews: dict[str, dict[str, Any]] = {}

    def state(self) -> dict[str, Any]:
        return {'groups': self.bot.permissions.list_groups(), 'profiles': copy.deepcopy(self.bot.bots.items),
                'overrides': self.bot.config_store.overrides}

    def identity(self, kind: str, source: str, target: str) -> str:
        if source == target:
            raise ValueError('目标资源必须与原资源不同')
        versions = []
        if kind == 'persona':
            self.bot.personas.reload()
            for name in (source, target):
                persona = self.bot.personas.find(name)
                if persona is None:
                    raise ValueError('原人格或目标人格已不存在，请重新读取资源列表')
                versions.append(persona.revision)
        elif any(name not in self.bot.config.llm.provider_names() for name in (source, target)):
            raise ValueError('原模型或目标模型已不存在，请重新读取资源列表')
        data = [kind, source, target, versions, self.bot.config_revision(), self.state()]
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def preview(self, kind: str, source: str, target: str) -> dict[str, Any]:
        async with self.bot.storage.settings_lock:
            identity = self.identity(kind, source, target)
            runtime = self.state()
            state = await resource_store.read(self.bot.storage.path)
            if (state['groups'] != runtime['groups'] or state['profiles'] != runtime['profiles']
                    or state['overrides'] and state['overrides'] != runtime['overrides']):
                raise ValueError('数据库与运行配置不同，请重新加载实例后预览；本次未修改引用')
            items = resource_references(self.bot, kind, source)
            preview_id = uuid.uuid4().hex
            result = {'id': preview_id, 'kind': kind, 'source': source, 'target': target,
                      'expires_at': time.time()+600, 'count': len(items),
                      'items': items[:self.MAX_REFERENCES], 'truncated': len(items)>self.MAX_REFERENCES,
                      'can_execute': 0 < len(items) <= self.MAX_REFERENCES,
                      'note': '只更换直接引用，继承场景随上层设置生效；保留原资源，不修改密钥、历史或其他插件数据。'}
            self.previews[preview_id] = {'result': result, 'identity': identity, 'state': state}
            if len(self.previews)>20:
                self.previews.pop(next(iter(self.previews)))
            return copy.deepcopy(result)

    def prepare(self, kind: str, source: str, target: str, state: dict[str, Any]):
        candidate = copy.deepcopy(state)
        llm = self.bot.config.llm
        patch: dict[str, Any] = {}
        if kind == 'persona' and llm.persona == source:
            patch['persona'] = target
        if kind == 'provider':
            if llm.default == source:
                patch['default'] = target
            routes = {key: target if name == source else name for key, name in llm.routes.items()}
            if routes != llm.routes:
                patch['routes'] = routes
            fallbacks = list(dict.fromkeys(target if name == source else name for name in llm.fallback_providers))
            if fallbacks != llm.fallback_providers:
                patch['fallback_providers'] = fallbacks
        config, overrides = self.bot.config_store.prepare_patch({'llm': patch}) if patch else (self.bot.config, state['overrides'])
        candidate['overrides'] = overrides
        for profile in candidate['profiles'].values():
            defaults = profile.get('defaults') or {}
            if defaults.get(kind) == source:
                defaults[kind] = target
                BotDefaults.model_validate(defaults)
                profile['defaults'] = defaults
        for group in candidate['groups']:
            values = group['config'].get('llm' if kind == 'persona' else 'policy')
            if isinstance(values, dict) and values.get(kind) == source:
                values[kind] = target
        return config, candidate

    async def apply(self, preview_id: str) -> dict[str, Any]:
        async with self.bot.storage.settings_lock:
            preview = self.previews.pop(preview_id, None)
            if preview is None or preview['result']['expires_at'] < time.time():
                raise ValueError('替换预览不存在或已过期，请重新预览')
            result = preview['result']
            if not result['can_execute']:
                raise ValueError('没有直接引用或数量超过 1000 处，请逐项调整')
            kind, source, target = result['kind'], result['source'], result['target']
            if self.identity(kind, source, target) != preview['identity']:
                raise ValueError('资源或配置已变化，请重新预览；本次未修改引用')
            config, candidate = self.prepare(kind, source, target, preview['state'])
            transaction = asyncio.create_task(resource_store.replace(
                self.bot.storage.path, preview['state'], candidate,
                kind=kind, source=source, target=target, references=result['items']), name='fllbot-resource-replacement')
            cancelled = False
            while True:
                try:
                    change_id = await asyncio.shield(transaction)
                    break
                except asyncio.CancelledError:
                    if transaction.cancelled():
                        raise
                    cancelled = True
            # Publish all caches without yielding after the transaction commits.
            if candidate['overrides'] != preview['state']['overrides']:
                self.bot.config_store.restore(candidate['overrides'])
                self.bot._apply_runtime_config(config)
            self.bot.bots.items = candidate['profiles']
            self.bot.permissions.publish_groups(candidate['groups'])
            if cancelled:
                raise asyncio.CancelledError
            return {'ok': True, 'kind': kind, 'source': source, 'target': target,
                    'count': result['count'], 'change_id': change_id,
                    'note': '直接引用已整体替换，原资源保留。请重新读取受影响配置；处理中请求可能仍使用此前资源。'}
