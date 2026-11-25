"""Host orchestration for natural-language agent turns; plugin adapters only supply tools."""

from __future__ import annotations

import random
import time
from contextlib import nullcontext
from typing import Any

from .llm.agent_runtime import AGENT_RULES
from .llm.client import LLMError

# 群聊规则里的“判断是否插话/静默”只适用于自动参与；私聊每条消息都是明确请求。
PRIVATE_RULES = (
    '\n\n当前是 QQ 私聊：对方的每条消息都是明确对你说的，直接回应，不需要等待 @ 或唤醒词，'
    '也不存在群聊自动参与。私聊不使用静默判断，不要调用 agent__stay_silent，'
    '不要用 <NO_REPLY> 结束回复。'
)


class AgentConversationService:
    def __init__(self, *, triggers, storage, conversations, build_messages, runtime, send, settings=None, tasks=None):
        self.triggers = triggers
        self.storage = storage
        self.conversations = conversations
        self.build_messages = build_messages
        self.runtime = runtime
        self.send = send
        self.settings = settings
        self.tasks = tasks

    async def respond(self, ctx: Any, tools: Any, execute: Any, *, instructions: Any = None) -> None:
        decision = self.triggers.decide(ctx)
        ctx.trigger_decision = decision
        await self.storage.record_trigger(ctx, decision)
        ctx.trace_event("trigger", decision.reason, "passed" if decision.outcome == "accept" else "skipped")
        if decision.outcome != "accept":
            return
        # 会话场景由事件决定：群聊按群号，私聊按对方 QQ 号；历史、记忆、模型与发送都跟着场景走。
        private = bool(getattr(ctx, "is_private", False))
        scene = "private" if private else "group"
        scene_id = int(ctx.user_id if private else ctx.group_id or 0)
        text, record_text = decision.text, decision.record_text
        settings = decision.settings
        fixed_replies = decision.fixed_replies
        if fixed_replies:
            ctx.turn_input = record_text
            async with self.conversations.turn(ctx) as accepted:
                if not accepted:
                    ctx.stop()
                    return
                reply = random.choice(fixed_replies)
                await self.conversations.project_input(ctx, record_text)
                await self.send(ctx, reply, require_ai=False)
                self.triggers.observe_reply(ctx)
                ctx.stop()
                return

        ctx.turn_input = record_text
        async with self.conversations.turn(ctx) as accepted:
            if not accepted:
                ctx.stop()
                return
            with self.tasks.track(ctx) if self.tasks else nullcontext():
                settings = self.settings(ctx) if self.settings else settings
                definitions, registry = tools(ctx)
                consent_key = f"{ctx.self_id}:private:{scene_id}" if private else f"{ctx.self_id}:{scene_id}"
                history_since = await self.storage.kv_get("agent-consent-start", consent_key)
                if history_since is None:
                    history_since = await self.storage.kv_initialize(
                        "agent-consent-start", consent_key, time.time() + 0.001)
                messages = await self.build_messages(
                    scene,
                    scene_id,
                    text,
                    ctx=ctx,
                    history_since=float(history_since),
                    tools=definitions,
                    system_rules=AGENT_RULES + PRIVATE_RULES if private else AGENT_RULES,
                    agent_instructions=instructions(registry, definitions) if instructions else [],
                )
                record_text = ctx.context_input or record_text
                definitions = ctx.context_snapshot.tools()
                try:
                    result = await self.runtime.run(
                        messages,
                        scene=scene,
                        scene_id=scene_id,
                        ctx=ctx,
                        tools=definitions,
                        registry=registry,
                        execute=execute,
                        agent=True,
                        inbox=(lambda: self.tasks.drain(ctx)) if self.tasks else None,
                        pending=(lambda: self.tasks.pending(ctx)) if self.tasks else None,
                        progress=(lambda *args: self.tasks.progress(ctx, *args)) if self.tasks else None,
                    )
                    if self.tasks:
                        self.tasks.finish_input(ctx)
                except LLMError:
                    # 与普通对话路径一致：模型失败只记录原因、静默收尾，
                    # 不冒泡给 dispatcher 发通用 error_reply，避免模型服务故障时刷屏。
                    ctx.turn_reason = 'model_failed'
                    ctx.trace_event("model", "model_failed", "failed")
                    ctx.stop()
                    return
                if result.reason in ("explicit_silence", "tool_completed"):
                    ctx.turn_silent_reason = result.reason
                elif result.reason == "empty_response":
                    ctx.turn_reason = result.reason
                if result.reason == "context_cleared":
                    ctx.trace_event("trigger", "context_cleared", "skipped")
                    ctx.stop()
                    return
                if result.text or result.reply or result.spoken:
                    if not ctx.input_projected:
                        await self.conversations.project_input(ctx, record_text)
                        ctx.input_projected = True
                    if result.text:
                        await self.send(ctx, result.text, model_reply_mode=result.reply_mode)
                        self.triggers.observe_reply(ctx)
                    elif result.reply:
                        # 直出快路径：工具已完成用户可见交付，用现有发送回调发它给的一句确认。
                        await self.send(ctx, result.reply)
                        self.triggers.observe_reply(ctx)
                elif result.reason == "empty_response" and ctx.to_me:
                    ctx.turn_reason = "empty_response"
                    await self.send(ctx, "我暂时没能生成回复，请稍后再试。")
                elif result.reason == "tool_limit":
                    ctx.turn_reason = "tool_limit"
                    await self.send(ctx, "我暂时没能完成这次操作，请稍后再试。")
                else:
                    ctx.trace_event(
                        "trigger", "model_silent" if result.reason == "explicit_silence" else result.reason, "skipped"
                    )
                ctx.stop()
