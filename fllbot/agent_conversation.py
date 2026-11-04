"""Host orchestration for natural-language agent turns; plugin adapters only supply tools."""

from __future__ import annotations

import random
import time
from typing import Any

from .llm.agent_runtime import AGENT_RULES
from .llm.client import LLMError


class AgentConversationService:
    def __init__(self, *, triggers, storage, conversations, build_messages, runtime, send, settings=None):
        self.triggers = triggers
        self.storage = storage
        self.conversations = conversations
        self.build_messages = build_messages
        self.runtime = runtime
        self.send = send
        self.settings = settings

    async def respond(self, ctx: Any, tools: Any, execute: Any) -> None:
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
                ctx.stop()
                return

        ctx.turn_input = record_text
        async with self.conversations.turn(ctx) as accepted:
            if not accepted:
                ctx.stop()
                return
            settings = self.settings(ctx) if self.settings else settings
            definitions, registry = tools(ctx)
            history_since = await self.storage.kv_get("agent-consent-start", f"{ctx.self_id}:{ctx.group_id}")
            if history_since is None:
                history_since = time.time() + 0.001
                await self.storage.kv_set("agent-consent-start", f"{ctx.self_id}:{ctx.group_id}", history_since)
            messages = await self.build_messages(
                "group",
                ctx.group_id,
                text,
                ctx=ctx,
                history_since=float(history_since),
                tools=definitions,
                system_rules=AGENT_RULES,
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
                )
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
