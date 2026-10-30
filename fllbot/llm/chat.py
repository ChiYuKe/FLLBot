"""多轮对话服务：人格、上下文裁剪、限流、分条发送。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..onebot.models import MessageEvent
from .client import LLMClient, LLMError
from .personas import PersonaStore

if TYPE_CHECKING:  # pragma: no cover
    from ..app import FLLBot
    from ..plugin.context import Context


@dataclass
class ChatReply:
    """一次模型回复的结果。"""

    text: str
    persona: str
    model: str
    latency_ms: float = 0.0
    provider: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "persona": self.persona,
            "model": self.model,
            "provider": self.provider,
            "usage": dict(self.usage),
            "latency_ms": round(self.latency_ms, 1),
        }


class ChatService:
    """面向 QQ 会话的多轮聊天封装。"""

    def __init__(self, bot: FLLBot) -> None:
        self.bot = bot
        self.logger = logging.getLogger("fllbot.llm")
        self.client = LLMClient(lambda: self.bot.config.llm)
        self._locks: dict[str, asyncio.Lock] = {}
        self.requests = 0
        self.failures = 0
        self.last_error: str | None = None
        self.last_latency_ms: float = 0.0

    # -- 查询 ------------------------------------------------------------- #
    @property
    def config(self) -> Any:
        return self.bot.config.llm

    @property
    def personas(self) -> PersonaStore:
        return self.bot.personas

    @property
    def available(self) -> bool:
        config = self.config
        if not config.enabled:
            return False
        return any(item["configured"] for item in self.client.providers_status())

    def scene_enabled(self, scene: str, scene_id: int) -> bool:
        config = self.config
        if not config.enabled:
            return False
        if scene == "private":
            return bool(config.enable_private)
        override = self.bot.permissions.group_config(scene_id).get("llm")
        if isinstance(override, dict) and override.get("enabled") is False:
            return False
        return bool(config.enable_group)

    def persona_name(self, scene: str, scene_id: int) -> str:
        if scene == "group":
            override = self.bot.permissions.group_config(scene_id).get("llm")
            if isinstance(override, dict) and override.get("persona"):
                return str(override["persona"])
        return str(self.config.persona)

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
        await self.client.close()

    # -- 提示词 ----------------------------------------------------------- #
    def _variables(self, scene: str, scene_id: int, ctx: Context | None) -> dict[str, Any]:
        now = datetime.now()
        variables: dict[str, Any] = {
            "bot_name": "FLLBot",
            "scene": scene,
            "group_id": scene_id if scene == "group" else "",
            "user_id": ctx.user_id if ctx is not None else "",
            "user_name": ctx.nickname if ctx is not None else "",
            "nickname": ctx.nickname if ctx is not None else "",
            "time": now.strftime("%H:%M"),
            "date": now.strftime("%Y-%m-%d"),
            "weekday": "一二三四五六日"[now.weekday()],
        }
        return variables

    def system_prompt(self, scene: str, scene_id: int, ctx: Context | None = None, persona_name: str | None = None) -> str:
        config = self.config
        persona = self.personas.get(persona_name or self.persona_name(scene, scene_id))
        text = persona.render(**self._variables(scene, scene_id, ctx))
        extra = (config.system_extra or "").strip()
        if extra:
            text = f"{text}\n\n{extra}"
        return text

    async def build_messages(
        self,
        scene: str,
        scene_id: int,
        user_text: str,
        *,
        persona_name: str | None = None,
        ctx: Context | None = None,
        include_history: bool = True,
    ) -> list[dict[str, str]]:
        config = self.config
        system = {"role": "system", "content": self.system_prompt(scene, scene_id, ctx, persona_name)}
        user = {"role": "user", "content": user_text}

        history: list[dict[str, str]] = []
        if include_history and config.max_context_messages > 0:
            rows = await self.bot.storage.recent_chat(scene, scene_id, limit=config.max_context_messages)
            history = [{"role": role, "content": content} for role, content in rows if content]

        return self._trim(system, history, user, config.max_context_messages, config.max_context_chars)

    @staticmethod
    def _trim(
        system: dict[str, str],
        history: list[dict[str, str]],
        user: dict[str, str],
        max_messages: int,
        max_chars: int,
    ) -> list[dict[str, str]]:
        selected: list[dict[str, str]] = []
        total = len(system["content"]) + len(user["content"])
        for item in reversed(history):
            if len(selected) >= max_messages:
                break
            length = len(item["content"])
            if total + length > max_chars and selected:
                break
            selected.append(item)
            total += length
        selected.reverse()
        return [system, *selected, user]

    # -- 调用 ------------------------------------------------------------- #
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        provider: str | None = None,
        scene: str | None = None,
        scene_id: int = 0,
    ) -> ChatReply:
        started = time.perf_counter()
        self.requests += 1
        name = provider or self.config.route(scene=scene)
        try:
            result = await self.client.chat_result(messages, max_tokens=max_tokens, provider=name)
        except LLMError as exc:
            self.failures += 1
            self.last_error = str(exc)
            self.logger.warning("模型调用失败: %s", exc)
            await self._record_usage(
                scene or "",
                scene_id,
                name,
                "",
                {},
                (time.perf_counter() - started) * 1000,
                ok=False,
                error=str(exc),
            )
            raise
        latency = (time.perf_counter() - started) * 1000
        self.last_latency_ms = latency
        self.last_error = None
        await self._record_usage(scene or "", scene_id, result.provider, result.model, result.usage, latency, ok=True)
        return ChatReply(
            text=result.text,
            persona="",
            model=result.model,
            latency_ms=latency,
            provider=result.provider,
            usage=result.usage,
        )

    def _cost(self, provider: str, usage: dict[str, int]) -> float:
        try:
            _, config = self.config.resolve(provider)
        except KeyError:  # pragma: no cover - resolve 已兜底
            return 0.0
        prompt = int(usage.get("prompt_tokens", 0))
        completion = int(usage.get("completion_tokens", 0))
        return (prompt * config.input_price + completion * config.output_price) / 1_000_000

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
        try:
            await self.bot.storage.record_llm_usage(
                scene=scene,
                scene_id=scene_id,
                provider=provider,
                model=model,
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                latency_ms=latency_ms,
                cost=self._cost(provider, usage) if ok else 0.0,
                ok=ok,
                error=error,
            )
        except Exception:  # pragma: no cover - 统计失败不影响对话
            self.logger.exception("记录 LLM 用量失败")

    async def test(self, prompt: str, *, persona_name: str | None = None, provider: str | None = None) -> ChatReply:
        """管理端自测：不带历史，直接用默认人格回答。"""
        config = self.config
        system = {
            "role": "system",
            "content": self.system_prompt("private", 0, None, persona_name),
        }
        messages = [system, {"role": "user", "content": prompt}]
        self.requests += 1
        started = time.perf_counter()
        try:
            result = await self.client.chat_result(
                messages,
                max_tokens=config.max_tokens,
                provider=provider,
                task="chat",
            )
        except LLMError as exc:
            self.failures += 1
            self.last_error = str(exc)
            raise
        latency = (time.perf_counter() - started) * 1000
        self.last_latency_ms = latency
        self.last_error = None
        await self._record_usage("test", 0, result.provider, result.model, result.usage, latency, ok=True)
        return ChatReply(
            text=result.text,
            persona=persona_name or config.persona,
            model=result.model,
            latency_ms=latency,
            provider=result.provider,
            usage=result.usage,
        )

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
        if len(content) > 2000:
            content = content[:2000]

        cooldown = float(config.cooldown_seconds or 0)
        if cooldown > 0 and not self.bot.permissions.cooldown_ok(("llm", "chat", ctx.user_id), cooldown):
            self.logger.debug("用户 %s 触发冷却，跳过", ctx.user_id)
            return None

        persona = persona_name or self.persona_name(scene, scene_id)
        lock = self._locks.setdefault(f"{scene}:{scene_id}", asyncio.Lock())
        async with lock:
            try:
                messages = await self.build_messages(scene, scene_id, content, persona_name=persona, ctx=ctx)
                reply = await self.complete(messages, max_tokens=config.max_tokens, scene=scene, scene_id=scene_id)
            except LLMError:
                return None

            reply.persona = persona
            try:
                await self.bot.storage.add_chat_message(scene, scene_id, "user", content)
                await self.bot.storage.add_chat_message(scene, scene_id, "assistant", reply.text)
            except Exception:  # pragma: no cover - 存历史失败不影响回复
                self.logger.exception("保存聊天历史失败")

            for index, chunk in enumerate(split_text(reply.text, config.chunk_size)):
                if index:
                    await asyncio.sleep(0.35)
                await ctx.reply(chunk)
            return reply

    async def clear(self, scene: str, scene_id: int) -> int:
        return await self.bot.storage.clear_chat(scene, scene_id)


def split_text(text: str, size: int) -> list[str]:
    """按长度切分长回复，尽量在标点处断开。"""
    text = (text or "").strip()
    if not text:
        return []
    if size <= 0 or len(text) <= size:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > size:
        window = remaining[:size]
        cut = max(
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind("；"),
            window.rfind("，"),
            window.rfind(" "),
        )
        if cut < size // 2:
            cut = size
        else:
            cut += 1
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        chunks.append(remaining.strip())
    return chunks


__all__ = ["ChatReply", "ChatService", "split_text"]
