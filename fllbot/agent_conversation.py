"""Host orchestration for natural-language agent turns; plugin adapters only supply tools."""

from __future__ import annotations

import random
import time
from contextlib import nullcontext
from typing import Any

from .llm.agent_runtime import AGENT_RULES
from .llm.client import LLMError


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
                history_since = await self.storage.kv_get("agent-consent-start", f"{ctx.self_id}:{ctx.group_id}")
                if history_since is None:
                    history_since = await self.storage.kv_initialize(
                        "agent-consent-start", f"{ctx.self_id}:{ctx.group_id}", time.time() + 0.001)
                messages = await self.build_messages(
                    "group",
                    ctx.group_id,
                    text,
                    ctx=ctx,
                    history_since=float(history_since),
                    tools=definitions,
                    system_rules=AGENT_RULES,
                    agent_instructions=instructions(registry, definitions) if instructions else [],
                )
                record_text = ctx.context_input or record_text
                definitions = ctx.context_snapshot.tools()
                try:
                    result = await self.runtime.run(
                        messages,
                        scene="group",
                        scene_id=ctx.group_id,
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
                    ctx.turn_reason = 'model_failed'
                    raise
                if result.reason in ("explicit_silence", "tool_completed"):
                    ctx.turn_silent_reason = result.reason
                elif result.reason == "empty_response":
                    ctx.turn_reason = result.reason
                if result.reason == "context_cleared":
                    ctx.trace_event("trigger", "context_cleared", "skipped")
                    ctx.stop()
                    return
                if result.text or result.spoken:
                    if not ctx.input_projected:
                        await self.conversations.project_input(ctx, record_text)
                        ctx.input_projected = True
                    if result.text:
                        await self.send(ctx, result.text)
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
