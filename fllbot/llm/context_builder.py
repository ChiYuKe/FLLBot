"""Immutable context snapshots; callers receive independent message copies."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextSnapshot:
    serialized_messages: str
    history_cutoff: float
    config_version: str
    persona_version: str
    provider: str
    model: str
    serialized_tools: str = '[]'
    serialized_sources: str = '[]'
    serialized_budget: str = '{}'
    request_version: str = ''

    def messages(self) -> list[dict]:
        return json.loads(self.serialized_messages)

    def tools(self) -> list[dict]:
        return json.loads(self.serialized_tools)

    def sources(self) -> list[dict]:
        return json.loads(self.serialized_sources)

    def budget(self) -> dict:
        return json.loads(self.serialized_budget)

    def metadata(self) -> dict:
        counts = {}
        for source in self.sources():
            kind = source['kind']
            counts[kind] = counts.get(kind,0) + 1
        return {'request_version':self.request_version,'config_version':self.config_version,
                'persona_version':self.persona_version,'history_cutoff':self.history_cutoff,
                'provider':self.provider,'model':self.model,'budget':self.budget(),'source_counts':counts,
                'tool_names':[tool.get('function',{}).get('name','') for tool in self.tools()]}


class ContextBuilder:
    def __init__(self, assemble: Any, configuration: Any, resolve_provider: Any, budget: Any = None, version: Any = None) -> None:
        self.assemble = assemble
        self.configuration = configuration
        self.resolve_provider = resolve_provider
        self.describe_budget = budget
        self.effective_version = version

    async def build(self, scene: str, scene_id: int, text: str, **options) -> ContextSnapshot:
        config = self.configuration()
        tools = json.dumps(sorted(options.pop('tools', None) or [],
            key=lambda item: item.get('function', {}).get('name', '')), ensure_ascii=False, sort_keys=True)
        rules = options.pop('system_rules', '')
        instructions = json.loads(json.dumps(options.pop('agent_instructions', []), ensure_ascii=False))
        version = self.effective_version() if self.effective_version else hashlib.sha256(config.model_dump_json().encode()).hexdigest()
        cutoff = time.time()
        provider, model = self.resolve_provider(scene, scene_id, options.get('ctx'), options.get('provider'))
        messages = await self.assemble(scene, scene_id, text, history_before=cutoff, **options)
        # Shared rules precede personas that may interpolate user/time variables.
        sections = ''.join(f"\n\n[插件策略 {item['source']}]\n{item['text']}"
                           for item in sorted(instructions, key=lambda item: item['source']))
        prefix = rules + sections
        if prefix:
            messages = [{**messages[0], 'content': prefix + '\n\n' + messages[0]['content']}, *messages[1:]]
        sources = []
        for index, message in enumerate(messages):
            source = message.get('_fllbot_source') or {
                'kind':'system' if index == 0 else 'current' if index == len(messages)-1 else 'history'}
            sources.append({'index':index,'self_id':options['ctx'].self_id if options.get('ctx') else 0,
                            'scene':scene,'scene_id':scene_id, **source})
        ctx = options.get('ctx')
        for item in instructions:
            sources.append({'index': 0, 'kind': 'plugin_instruction', 'source': item['source']})
        if ctx is not None and ctx.quoted_message is not None:
            quote = ctx.quoted_message
            sources[-1]['quote'] = {'message_id':quote.message_id,'user_id':quote.user_id,'error':quote.error}
        if ctx is not None and ctx.forward_metadata:
            sources.append({'index':len(messages)-1,'self_id':ctx.self_id,'scene':scene,'scene_id':scene_id,
                            **ctx.forward_metadata})
        budget = await self.describe_budget(provider, messages, json.loads(tools)) if self.describe_budget else {}
        serialized = json.dumps(messages, ensure_ascii=False)
        persona = hashlib.sha256(str(messages[0]['content']).encode()).hexdigest()
        budget = json.dumps(budget, ensure_ascii=False, sort_keys=True)
        sources = json.dumps(sources, ensure_ascii=False, sort_keys=True)
        request_version = hashlib.sha256((serialized + tools + budget + sources + version).encode()).hexdigest()
        return ContextSnapshot(serialized, cutoff, version, persona, provider, model, tools, sources, budget, request_version)
