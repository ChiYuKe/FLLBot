"""兼容聊天入口：协调上下文快照、Agent 执行与回复服务。"""

from __future__ import annotations

import copy
import hashlib
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ..conversation import ConversationKey, ConversationLockPool
from ..onebot.models import MessageEvent
from .client import LLMClient, LLMError
from .context_assembly import ContextAssembler
from .context_builder import ContextBuilder
from .execution_config import ExecutionConfig, current, execution_scope
from .message_format import group_user_content, speaker_label
from .model_gateway import ChatReply, ModelGateway
from .personas import PersonaStore
from .quote import QuotedMessage
from .reply import split_text

if TYPE_CHECKING:  # pragma: no cover
    from ..app import FLLBot
    from ..plugin.context import Context


class ChatService:
    """面向 QQ 会话的多轮聊天封装。"""

    def __init__(
        self,
        bot: FLLBot,
        *,
        memory: Any = None,
        gateway: ModelGateway | None = None,
        assembler: ContextAssembler | None = None,
        builder: ContextBuilder | None = None,
    ) -> None:
        self.bot = bot
        self.logger = logging.getLogger("fllbot.llm")
        self.client = LLMClient(lambda: self.execution_config.llm)
        self._conversation_pool = ConversationLockPool()
        from .memory import GroupMemory

        self.memory = memory if memory is not None else GroupMemory(bot)
        self.gateway = gateway or ModelGateway(
            configuration=lambda: self.config,
            client=lambda: self.client,
            storage=bot.storage,
            provider_name=self.provider_name,
            execution_scope=self.execution_scope,
        )
        self.context_assembler = assembler or ContextAssembler(
            configuration=lambda: self.execution_config,
            personas=lambda: self.personas,
            persona_name=self.persona_name,
            provider_name=self.provider_name,
            policy=self._context_policy,
            nickname=bot.bots.nickname,
            client=lambda: self.client,
            history=bot.storage.recent_chat,
            memory=self.memory,
        )
        self.context_builder = builder or ContextBuilder(
            self.context_assembler.assemble,
            lambda: self.execution_config,
            self.context_assembler.resolve_provider,
            self.gateway.describe_budget,
            version=lambda:self.execution_version,
        )

    def _context_policy(self, scene_id: int) -> dict:
        from ..policy import policy_overrides

        snapshot = current(self.bot)
        return snapshot.policy if snapshot else policy_overrides(self.bot, scene_id)

    @property
    def requests(self):
        return self.gateway.requests

    @property
    def failures(self):
        return self.gateway.failures

    @property
    def last_error(self):
        return self.gateway.last_error

    @property
    def last_latency_ms(self):
        return self.gateway.last_latency_ms

    def conversation_lock(self, self_id: int, scene: str, scene_id: int):
        key = ConversationKey("qq", self_id, scene, scene_id)
        return self._conversation_pool.lease(key)

    @property
    def execution_config(self) -> Any:
        snapshot = current(self.bot)
        return snapshot.config if snapshot else self.bot.config

    @property
    def execution_version(self) -> str:
        snapshot = current(self.bot)
        return snapshot.version if snapshot else hashlib.sha256(self.bot.config.model_dump_json().encode()).hexdigest()

    def turn_agent_settings(self, group_id: int, self_id: int | None = None, *, live: bool = False) -> dict:
        from ..policy import agent_settings
        snapshot = current(self.bot)
        if not live and snapshot and snapshot.scene == 'group' and snapshot.scene_id == group_id:
            return copy.deepcopy(snapshot.agent)
        return agent_settings(self.bot,group_id,self_id)

    @contextmanager
    def execution_scope(self, scene: str, scene_id: int, self_id: int | None = None, config: Any = None):
        if current(self.bot) is not None:
            yield
            return
        from ..policy import agent_settings, policy_overrides

        snapshot = ExecutionConfig(
            id(self.bot),
            (config or self.bot.config).model_copy(deep=True),
            scene,
            scene_id,
            copy.deepcopy(self.bot.permissions.group_config(scene_id)) if scene == "group" else {},
            copy.deepcopy(policy_overrides(self.bot, scene_id)) if scene == "group" else {},
            self.bot.bots.defaults(self_id).model_copy(deep=True),
            copy.deepcopy(self.bot.personas),
            copy.deepcopy(agent_settings(self.bot,scene_id,self_id)) if scene == 'group' else {},
        )
        with execution_scope(snapshot):
            yield

    def scope_for_turn(self, ctx: Context, config: Any):
        return self.execution_scope(ctx.event.scene, ctx.event.scene_id, ctx.self_id, config)

    def _group_config(self, scene_id: int) -> dict:
        snapshot = current(self.bot)
        if snapshot and snapshot.scene == "group" and snapshot.scene_id == scene_id:
            return snapshot.group_config
        return self.bot.permissions.group_config(scene_id)

    # -- 查询 ------------------------------------------------------------- #
    @property
    def config(self) -> Any:
        return self.execution_config.llm

    @property
    def personas(self) -> PersonaStore:
        snapshot = current(self.bot)
        return snapshot.personas if snapshot else self.bot.personas

    @property
    def available(self) -> bool:
        config = self.bot.config.llm
        if not config.enabled:
            return False
        return any(item["configured"] for item in self.client.providers_status())

    def scene_enabled(self, scene: str, scene_id: int) -> bool:
        config = self.bot.config.llm
        if not config.enabled:
            return False
        if scene == "private":
            return bool(config.enable_private)
        override = self.bot.permissions.group_config(scene_id).get("llm")
        if isinstance(override, dict) and override.get("enabled") is False:
            return False
        return bool(config.enable_group)

    def persona_name(self, scene: str, scene_id: int, self_id: int | None = None) -> str:
        if scene == "group":
            override = self._group_config(scene_id).get("llm")
            if isinstance(override, dict) and override.get("persona"):
                return str(override["persona"])
        return (current(self.bot).defaults if current(self.bot) else self.bot.bots.defaults(self_id)).persona or str(
            self.config.persona
        )

    def provider_name(
        self, scene: str | None, scene_id: int, task: str | None = None, self_id: int | None = None
    ) -> str:
        if scene == "group":
            from ..policy import policy_overrides

            name = (current(self.bot).policy if current(self.bot) else policy_overrides(self.bot, scene_id)).get(
                "provider"
            )
            if name:
                if name not in self.config.provider_names():
                    raise LLMError("群策略指定的模型服务已不可用，请调整该群策略")
                return str(name)
        name = (current(self.bot).defaults if current(self.bot) else self.bot.bots.defaults(self_id)).provider
        if name:
            if name not in self.config.provider_names():
                raise LLMError("Bot 默认模型服务已不可用，请调整机器人设置")
            return name
        return self.config.route(scene=scene, task=task)

    def status(self) -> dict[str, Any]:
        config = self.config
        name, provider = config.resolve(config.route())
        return {
            "enabled": bool(config.enabled),
            "available": self.available,
            "model": provider.model,
            "base_url": provider.base_url,
            "provider": name,
            "providers": self.client.providers_status(),
            "trigger": config.trigger,
            "persona": config.persona,
            "requests": self.requests,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_latency_ms": round(self.last_latency_ms, 1),
            "personas": self.personas.names(),
        }

    async def close(self) -> None:
        await self.memory.close()
        await self.client.close()

    # -- 提示词 ----------------------------------------------------------- #
    def _variables(self, scene: str, scene_id: int, ctx: Context | None) -> dict[str, Any]:
        return self.context_assembler._variables(scene, scene_id, ctx)

    def system_prompt(
        self, scene: str, scene_id: int, ctx: Context | None = None, persona_name: str | None = None
    ) -> str:
        return self.context_assembler.system_prompt(scene, scene_id, ctx, persona_name)

    def _context_provider(
        self, scene: str, scene_id: int, ctx: Context | None, provider: str | None = None
    ) -> tuple[str, str]:
        return self.context_assembler.resolve_provider(scene, scene_id, ctx, provider)

    async def build_messages(self, scene: str, scene_id: int, user_text: str, **options) -> list[dict[str, Any]]:
        ctx = options.get("ctx")
        with self.execution_scope(scene, scene_id, ctx.self_id if ctx else None):
            snapshot = await self.context_builder.build(scene, scene_id, user_text, **options)
            ctx = options.get("ctx")
            if ctx is not None:
                ctx.context_snapshot = snapshot
                await self.bot.storage.set_turn_context(ctx.turn_id, snapshot.metadata())
            return snapshot.messages()

    async def _context_budget(self, provider: str, messages: list, tools: list) -> dict:
        return await self.gateway.describe_budget(provider, messages, tools)

    async def _build_messages(
        self,
        scene: str,
        scene_id: int,
        user_text: str,
        *,
        persona_name: str | None = None,
        ctx: Context | None = None,
        include_history: bool = True,
        history_since: float | None = None,
        history_before: float | None = None,
        provider: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.context_assembler.assemble(
            scene,
            scene_id,
            user_text,
            persona_name=persona_name,
            ctx=ctx,
            include_history=include_history,
            history_since=history_since,
            history_before=history_before,
            provider=provider,
        )

    async def _forward_content(self, ctx: Context, text: str) -> str:
        return await self.context_assembler._forward_content(ctx, text)

    def _forward_sender(self, sender: dict, node: dict, body: str) -> str:
        return self.context_assembler._forward_sender(sender, node, body)

    def _quote_content(self, quoted: QuotedMessage, user_content: str) -> tuple[str, list[str]]:
        return self.context_assembler._quote_content(quoted, user_content)

    def _group_content(
        self, user_id: int | None, nickname: str | None, role: str | None, text: str, timestamp: float = 0
    ) -> str:
        return self.context_assembler._group_content(user_id, nickname, role, text, timestamp)

    def _current_images(self, ctx: Context | None) -> list[str]:
        return self.context_assembler._current_images(ctx)

    def _turn_images(self, ctx: Context | None, images: list[str] | None = None) -> list[str]:
        return self.context_assembler._turn_images(ctx, images)

    def _with_images(
        self, messages: list[dict[str, Any]], ctx: Context | None, images: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return self.context_assembler._with_images(messages, ctx, images)

    def _history_entry(self, scene: str, record: Any) -> dict[str, str]:
        return self.context_assembler._history_entry(scene, record)

    @staticmethod
    def _trim(
        system: dict[str, str],
        history: list[dict[str, str]],
        user: dict[str, str],
        max_messages: int,
        max_chars: int,
    ) -> list[dict[str, str]]:
        return ContextAssembler._trim(system, history, user, max_messages, max_chars)

    # -- 调用 ------------------------------------------------------------- #
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        unbounded_output: bool = False,
        provider: str | None = None,
        scene: str | None = None,
        scene_id: int = 0,
        task: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        self_id: int | None = None,
        allow_fallback: bool = True,
    ) -> ChatReply:
        return await self.gateway.complete(
            messages,
            max_tokens=max_tokens,
            unbounded_output=unbounded_output,
            provider=provider,
            scene=scene,
            scene_id=scene_id,
            task=task,
            tools=tools,
            self_id=self_id,
            allow_fallback=allow_fallback,
        )

    async def _complete_with_quota(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        unbounded_output: bool = False,
        provider: str | None = None,
        scene: str | None = None,
        scene_id: int = 0,
        task: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        self_id: int | None = None,
        allow_fallback: bool = True,
    ) -> ChatReply:
        return await self.gateway._complete_with_quota(
            messages,
            max_tokens=max_tokens,
            unbounded_output=unbounded_output,
            provider=provider,
            scene=scene,
            scene_id=scene_id,
            task=task,
            tools=tools,
            self_id=self_id,
            allow_fallback=allow_fallback,
        )

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        unbounded_output: bool = False,
        provider: str | None = None,
        scene: str | None = None,
        scene_id: int = 0,
        task: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        self_id: int | None = None,
        allow_fallback: bool = True,
    ) -> ChatReply:
        return await self.gateway._complete(
            messages,
            max_tokens=max_tokens,
            unbounded_output=unbounded_output,
            provider=provider,
            scene=scene,
            scene_id=scene_id,
            task=task,
            tools=tools,
            self_id=self_id,
            allow_fallback=allow_fallback,
        )

    def _cost(self, provider: str, usage: dict[str, int]) -> float:
        return self.gateway._cost(provider, usage)

    async def _record_usage(
        self,
        scene: str,
        scene_id: int,
        provider: str,
        model: str,
        usage: dict[str, int],
        latency_ms: float,
        *,
        ok: bool,
        error: str = "",
    ) -> None:
        return await self.gateway._record_usage(scene, scene_id, provider, model, usage, latency_ms, ok=ok, error=error)

    async def test(self, prompt: str, *, persona_name: str | None = None, provider: str | None = None) -> ChatReply:
        """管理端自测：不带历史，直接用默认人格回答。"""
        config = self.config
        system = {
            "role": "system",
            "content": self.system_prompt("private", 0, None, persona_name),
        }
        messages = [system, {"role": "user", "content": prompt}]
        reply = await self.complete(messages, provider=provider, scene="test", task="chat")
        reply.persona = persona_name or config.persona
        return reply

    async def generate(
        self,
        scene: str,
        scene_id: int,
        text: str,
        *,
        ctx: Context | None = None,
        persona_name: str | None = None,
        provider: str | None = None,
        include_history: bool = True,
    ) -> tuple[ChatReply, int]:
        with self.execution_scope(scene, scene_id, ctx.self_id if ctx else None):
            messages = await self.build_messages(
                scene,
                scene_id,
                text,
                ctx=ctx,
                persona_name=persona_name,
                include_history=include_history,
                provider=provider,
            )
            result = await self.bot.agent_runtime.run(
                messages, provider=provider, scene=scene, scene_id=scene_id, ctx=ctx
            )
            if result.response is None:
                raise LLMError("上下文已失效")
            return result.response, len(messages)

    async def preview(
        self,
        scene: str,
        scene_id: int,
        text: str,
        *,
        persona_name: str | None = None,
        provider: str | None = None,
        include_history: bool = True,
        save: bool = False,
        self_id: int = 0,
    ) -> dict:
        if self_id and save:
            raise ValueError("真实账号调试为只读模式，不能写入 QQ 会话历史")
        if self_id and str(self_id) not in self.bot.bots.items and self.bot.manager.by_self_id(self_id) is None:
            raise ValueError("账号不存在，请选择已连接过的账号")
        ctx = self.bot.debug_context(scene, scene_id, self_id)
        ctx.turn_input = text
        async with self.bot.conversations.turn(ctx) as accepted:
            if not accepted:
                raise ValueError("调试轮次已失效，请重新测试")
            reply, count = await self.generate(
                scene,
                scene_id,
                text,
                ctx=ctx,
                persona_name=persona_name,
                provider=provider,
                include_history=include_history,
            )
            if save:
                async with self.bot.storage.data_lock:
                    if not ctx.data_current:
                        raise ValueError("会话数据已清理，本次测试结果未保存，请重新测试")
                    await self.bot.storage.add_chat_message(scene, scene_id, "user", text, generation=ctx.data_epoch)
                    await self.bot.storage.add_chat_message(
                        scene, scene_id, "assistant", reply.text, generation=ctx.data_epoch
                    )
            return {**reply.summary(), "messages": count, "turn_id": ctx.turn_id}

    # -- QQ 对话 ---------------------------------------------------------- #
    async def respond(self, ctx: Context, text: str, *, persona_name: str | None = None) -> ChatReply | None:
        """处理一条需要模型回复的消息；返回 ``None`` 表示未回复。"""
        if not self.available:
            return None
        if not isinstance(ctx.event, MessageEvent):
            return None

        scene = ctx.event.scene
        scene_id = ctx.event.scene_id
        if not self.scene_enabled(scene, scene_id):
            return None

        config = self.config
        content = (text or "").strip()
        if not content:
            return None

        cooldown = float(config.cooldown_seconds or 0)
        if cooldown > 0 and not self.bot.permissions.cooldown_ok(("llm", "chat", ctx.user_id), cooldown):
            self.logger.debug("用户 %s 触发冷却，跳过", ctx.user_id)
            return None

        persona = persona_name or self.persona_name(scene, scene_id, ctx.self_id)
        ctx.turn_input = content
        async with self.bot.conversations.turn(ctx) as accepted:
            if not accepted:
                return None
            try:
                reply, _ = await self.generate(scene, scene_id, content, persona_name=persona, ctx=ctx)
            except LLMError:
                ctx.turn_reason = "model_failed"
                return None

            reply.persona = persona
            if not ctx.data_current:
                ctx.trace_event("trigger", "context_cleared", "skipped")
                return None
            try:
                await self.bot.conversations.project_input(ctx,ctx.context_input or content)
            except Exception:  # pragma: no cover - 存历史失败不影响回复
                self.logger.exception("保存聊天历史失败")

            from .output import send_chat_reply

            await send_chat_reply(ctx, reply.text)
            return reply

    async def clear(self, scene: str, scene_id: int, self_id: int = 0) -> int:
        return await self.bot.storage.clear_chat(scene, scene_id, self_id=self_id)


__all__ = ["ChatReply", "ChatService", "group_user_content", "speaker_label", "split_text"]
