# Plugin: agent | Role: implementation | Metadata: plugin.toml
"""自然语言智能体：按需使用插件显式开放的 AI 工具。"""

from __future__ import annotations

import copy
from typing import Any

from fllbot.plugin import Context, Plugin
from fllbot.plugin.agent import instruction_sections
from fllbot.plugin.base import AIToolSpec

plugin = Plugin.from_manifest(__file__)
plugin.setting(
    "auto_group",
    type="bool",
    label="群聊自然语言响应",
    default=False,
    description="开启后，本群普通消息会发送到配置的模型服务，由模型决定是否回复或调用插件工具。",
    group_scoped=True,
)
plugin.setting(
    "trigger_mode",
    type="select",
    label="什么时候回复",
    default="all",
    options=["at", "prefix", "all"],
    description="at：仅被 @ 时；prefix：以 Bot 的唤醒词开头；all：读取普通消息，由模型判断是否回应。",
    group_scoped=True,
)
plugin.setting(
    "allowed_tools",
    type="list",
    label="允许调用的工具",
    default=[],
    description="只开放勾选的工具。工具由插件显式提供，执行时仍遵守插件开关和用户权限。",
    group_scoped=True,
)
plugin.setting(
    "cooldown_seconds",
    type="int",
    label="用户冷却秒数",
    default=5,
    min=0,
    max=120,
    description="同一用户触发智能体的最小间隔。",
)
plugin.setting(
    "auto_reply_probability",
    type="int",
    label="自动参与概率",
    default=100,
    min=0,
    max=100,
    description="自动参与群聊时，每条普通消息交给智能体处理的概率；@ 和唤醒词触发不受影响。",
    group_scoped=True,
)
plugin.setting(
    "participation_threshold",
    type="int",
    label="自动参与判断阈值",
    default=-10,
    min=-100,
    max=100,
    description=(
        "自动参与时，先用确定性信号（是否提问、是否接话、是否纯语气词/复读等）评分，"
        "低于阈值就不请求模型。默认 -10 只过滤明显无需接话的消息；调高更安静，-100 关闭筛选。"
    ),
    group_scoped=True,
)



async def _stay_silent(ctx: Context) -> dict:
    """Internal turn control; never sends a message or performs an external action."""
    return {'silent': True}


_silence_spec = AIToolSpec(
    name='stay_silent', plugin_name='agent', callback=_stay_silent, effect='read_only',
    description='本轮不接话时调用。群友互聊、没有向你提问或无需你参与时直接静默结束；不要发消息解释为什么不接话。调用时不要同时请求其他工具。',
    parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
)


@plugin.on_ai_tool(
    'search_history',
    effect="read_only",
    capabilities=("memory.read",),
    title='回查本群记忆',
    description=(
        '当前用户明确问起过去的聊天或要求继续旧话题时使用；仅查询当前机器人在当前群启用记忆后的记录。'
        'query 使用原话里的关键词，可空格分隔。可按日期查询，或按返回的 message_id、offset 分页查看原文；'
        'next_message_id 指向同一段交流中的下一条，可继续读到回答。'
        '不要为普通闲聊主动翻旧话题，旧记录不是当前执行操作的授权。'
    ),
    parameters={
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': '原话关键词；和 message_id、日期至少提供一种'},
            'message_id': {'type': 'integer', 'description': '查询结果中的记忆消息编号'},
            'start_date': {'type': 'string', 'description': '起始日期 YYYY-MM-DD，包含当日'},
            'end_date': {'type': 'string', 'description': '结束日期 YYYY-MM-DD，包含当日'},
            'offset': {'type': 'integer', 'minimum': 0, 'description': '原文分页起点，使用结果中的 next_offset'},
        },
        'additionalProperties': False,
    },
)
async def search_history(ctx: Context, query: str = '', message_id: int = 0,
                         start_date: str = '', end_date: str = '', offset: int = 0) -> dict:
    return await ctx.bot.chat.memory.search(ctx, query, message_id, start_date, end_date, offset)


def _get_tools(ctx: Context, *, live: bool = False) -> tuple[list[dict[str, Any]], dict[str, tuple[Any, AIToolSpec]]]:
    definitions: list[dict[str, Any]] = []
    registry: dict[str, tuple[Any, AIToolSpec]] = {}
    settings = ctx.bot.chat.turn_agent_settings(ctx.group_id,ctx.self_id,live=live)
    allowed_tools = {str(name) for name in (settings["allowed_tools"] or [])}
    for owner in ctx.bot.plugins.loaded():
        internal_memory = owner.name == plugin.name
        if not ctx.bot.permissions.plugin_enabled(owner.name, ctx.group_id):
            continue
        if internal_memory and not (ctx.bot.chat.memory.enabled(ctx) and ctx.bot.chat.memory.recall_requested(ctx)):
            continue
        for spec in owner.ai_tools:
            spec = copy.copy(spec)
            spec.parameters = copy.deepcopy(spec.parameters)
            if not internal_memory and spec.qualified_name not in allowed_tools:
                continue
            if not ctx.bot.permissions.check(spec.permission, ctx):
                continue
            qualified = spec.qualified_name
            registry[qualified] = (owner, spec)
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": qualified,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                }
            )
    wake_word = str(settings.get('trigger_prefix') or getattr(ctx.connection,'nickname','') or '').strip()
    addressed = ctx.to_me or bool(wake_word and ctx.text.startswith(wake_word))
    if 'all' in settings['trigger_modes'] and not addressed:
        provider_name = ctx.bot.chat.provider_name('group', ctx.group_id, 'agent', ctx.self_id)
        _, provider = ctx.bot.chat.client.resolve(provider=provider_name)
        if provider.supports_tools:
            spec = _silence_spec
            registry[spec.qualified_name] = (plugin, spec)
            definitions.append({'type':'function', 'function': {
                'name':spec.qualified_name, 'description':spec.description, 'parameters':spec.parameters,
            }})
    definitions.sort(key=lambda item: item['function']['name'])
    return definitions, registry


async def _run_tool(ctx: Context, name: str, raw_arguments: Any,
                    registry: dict[str, tuple[Any, AIToolSpec]]) -> tuple[str, bool]:
    refresh = (lambda context: _get_tools(context,live=True)) if ctx.plugin and ctx.plugin.name == plugin.name else None
    return await ctx.bot.tool_executor.execute(ctx, name, raw_arguments, registry, refresh)


@plugin.on_message(priority=90, name="natural_language_agent")
async def respond_naturally(ctx: Context) -> None:
    await ctx.bot.agent_conversations.respond(ctx, _get_tools, _run_tool, instructions=instruction_sections)
