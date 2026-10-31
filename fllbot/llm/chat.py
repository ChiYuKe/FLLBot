"""多轮对话服务：人格、上下文裁剪、限流、分条发送。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..onebot.models import MessageEvent
from .client import DEFAULT_CONTEXT_WINDOW, LLMClient, LLMError
from .personas import PersonaStore

if TYPE_CHECKING:  # pragma: no cover
    from ..app import FLLBot
    from ..plugin.context import Context

_GROUP_ROLE_LABELS = {"owner": "群主", "admin": "管理员", "member": "成员"}


def speaker_label(user_id: int | None, nickname: str | None, sender_role: str | None = None) -> str:
    """群聊说话人标签：``昵称(QQ号，身份)``；没有可用信息时返回空串。"""
    name = (nickname or "").strip()
    uid = int(user_id or 0)
    if not name and uid:
        name = f"QQ {uid}"
    if not name:
        return ""
    tags = [str(uid)] if uid else []
    role_label = _GROUP_ROLE_LABELS.get(str(sender_role or "").strip().lower())
    if role_label:
        tags.append(role_label)
    return f"{name}({'，'.join(tags)})" if tags else name


def group_user_content(
    user_id: int | None,
    nickname: str | None,
    sender_role: str | None,
    content: str,
) -> str:
    """给群聊用户消息加上发送者标签，便于模型区分说话人。"""
    label = speaker_label(user_id, nickname, sender_role)
    return f"{label}：{content}" if label else content


@dataclass
class ChatReply:
    """一次模型回复的结果。"""

    text: str
    persona: str
    model: str
    latency_ms: float = 0.0
    provider: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

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

    def persona_name(self, scene: str, scene_id: int, self_id: int | None = None) -> str:
        if scene == "group":
            override = self.bot.permissions.group_config(scene_id).get("llm")
            if isinstance(override, dict) and override.get("persona"):
                return str(override["persona"])
        return self.bot.bots.defaults(self_id).persona or str(self.config.persona)

    def provider_name(self, scene: str | None, scene_id: int, task: str | None = None, self_id: int | None = None) -> str:
        if scene == "group":
            from ..policy import policy_overrides

            name = policy_overrides(self.bot, scene_id).get("provider")
            if name:
                if name not in self.config.provider_names():
                    raise LLMError("群策略指定的模型服务已不可用，请调整该群策略")
                return str(name)
        name = self.bot.bots.defaults(self_id).provider
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
        await self.client.close()

    # -- 提示词 ----------------------------------------------------------- #
    def _variables(self, scene: str, scene_id: int, ctx: Context | None) -> dict[str, Any]:
        now = datetime.now()
        options = self.bot.config.chat
        variables: dict[str, Any] = {
            "bot_name": (getattr(ctx.connection, "nickname", "") or self.bot.bots.nickname(ctx.self_id) or "FLLBot") if ctx else "FLLBot",
            "scene": scene,
            "group_id": scene_id if scene == "group" else "",
            "user_id": ctx.user_id if ctx is not None and options.send_user_id else "",
            "user_name": (ctx.sender.card or ctx.sender.nickname) if ctx is not None and options.send_nickname else "",
            "nickname": (ctx.sender.card or ctx.sender.nickname) if ctx is not None and options.send_nickname else "",
            "time": now.strftime("%H:%M") if options.send_time else "",
            "date": now.strftime("%Y-%m-%d") if options.send_time else "",
            "weekday": "一二三四五六日"[now.weekday()] if options.send_time else "",
        }
        return variables

    def system_prompt(self, scene: str, scene_id: int, ctx: Context | None = None, persona_name: str | None = None) -> str:
        config = self.config
        persona = self.personas.get(persona_name or self.persona_name(scene, scene_id, ctx.self_id if ctx else None))
        text = persona.render(**self._variables(scene, scene_id, ctx))
        extra = (config.system_extra or "").strip()
        if extra:
            text = f"{text}\n\n{extra}"
        if scene == "group":
            from ..policy import policy_overrides

            group_extra = policy_overrides(self.bot, scene_id).get("system_extra", "").strip()
            if group_extra:
                text = f"{text}\n\n本群补充规则：\n{group_extra}"
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
        history_since: float | None = None,
    ) -> list[dict[str, Any]]:
        config = self.config
        system = {"role": "system", "content": self.system_prompt(scene, scene_id, ctx, persona_name)}
        user_content = user_text
        if scene == "group" and ctx is not None:
            user_content = self._group_content(ctx.user_id, ctx.nickname, ctx.sender.role, user_text, getattr(ctx.event, "time", 0))
        user = {"role": "user", "content": user_content}

        history: list[dict[str, str]] = []
        if config.context_mode == "model":
            # Respect an explicit user setting first, then provider metadata, then
            # the conservative fallback. Metadata is never inferred from names.
            selected_provider = self.provider_name(scene, scene_id, "agent" if ctx and ctx.plugin and ctx.plugin.name == "agent" else None, ctx.self_id if ctx else None)
            window = config.context_window or await self.client.context_window(scene=scene, provider=selected_provider) or DEFAULT_CONTEXT_WINDOW
            _, provider = self.client.resolve(provider=selected_provider, scene=scene)
            budget = max(0, window - (provider.max_tokens or config.max_tokens) - len(self._current_images(ctx)) * 2048)
            if include_history:
                rows = await self.bot.storage.recent_chat(
                    scene, scene_id, limit=window, created_after=history_since
                )
                history = [self._history_entry(scene, row) for row in rows if row.content]
            selected: list[dict[str, str]] = []
            # UTF-8 bytes are a conservative token upper bound, including CJK.
            used = sum(len(item["content"].encode("utf-8")) + 8 for item in (system, user))
            for item in reversed(history):
                size = len(item["content"].encode("utf-8")) + 8
                if used + size > budget:
                    break
                selected.append(item)
                used += size
            return self._with_images([system, *reversed(selected), user], ctx)
        if include_history and config.max_context_messages > 0:
            rows = await self.bot.storage.recent_chat(
                scene, scene_id, limit=config.max_context_messages, created_after=history_since
            )
            history = [self._history_entry(scene, row) for row in rows if row.content]

        return self._with_images(self._trim(system, history, user, config.max_context_messages, config.max_context_chars), ctx)

    def _group_content(self, user_id: int | None, nickname: str | None, role: str | None, text: str, timestamp: float = 0) -> str:
        options = self.bot.config.chat
        name = nickname if options.send_nickname else None
        if not options.send_user_id and name == str(user_id):
            name = None
        content = group_user_content(user_id if options.send_user_id else None, name, role if options.send_role else None, text)
        if not name and not (options.send_user_id and user_id) and options.send_role and role in _GROUP_ROLE_LABELS:
            content = _GROUP_ROLE_LABELS[role] + '：' + text
        if options.send_time and timestamp:
            content = '[' + datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S') + '] ' + content
        return content

    def _current_images(self, ctx: Context | None) -> list[str]:
        if not self.bot.config.chat.allow_images or ctx is None:
            return []
        result = []
        for segment in ctx.message:
            if segment.type != "image":
                continue
            url = str(segment.data.get("url") or segment.data.get("file") or "")
            if url.startswith(("https://", "http://", "data:image/")) and url not in result:
                result.append(url)
            if len(result) == 4:
                break
        return result

    def _with_images(self, messages: list[dict[str, Any]], ctx: Context | None) -> list[dict[str, Any]]:
        images = self._current_images(ctx)
        if images:
            messages[-1] = {**messages[-1], "content": [{"type": "text", "text": messages[-1]["content"]}, *[
                {"type": "image_url", "image_url": {"url": url}} for url in images
            ]]}
        return messages

    def _history_entry(self, scene: str, record: Any) -> dict[str, str]:
        """把存储的历史还原成模型消息；群聊用户消息带上发送者标签。"""
        content = record.content
        if scene == "group" and record.role == "user":
            content = self._group_content(record.user_id, record.nickname, record.sender_role, content, getattr(record, "created_at", 0))
        return {"role": record.role, "content": content}

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
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        provider: str | None = None,
        scene: str | None = None,
        scene_id: int = 0,
        task: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        self_id: int | None = None,
    ) -> ChatReply:
        started = time.perf_counter()
        self.requests += 1
        name = provider or self.provider_name(scene, scene_id, task, self_id)
        try:
            result = await self.client.chat_result(
                messages,
                max_tokens=max_tokens,
                provider=name,
                scene=scene,
                task=task,
                tools=tools,
            )
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
            tool_calls=result.tool_calls,
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

        persona = persona_name or self.persona_name(scene, scene_id, ctx.self_id)
        lock = self._locks.setdefault(f"{scene}:{scene_id}", asyncio.Lock())
        async with lock:
            try:
                messages = await self.build_messages(scene, scene_id, content, persona_name=persona, ctx=ctx)
                reply = await self.complete(messages, max_tokens=config.max_tokens, scene=scene, scene_id=scene_id, self_id=ctx.self_id)
            except LLMError:
                return None

            reply.persona = persona
            try:
                await self.bot.storage.add_chat_message(
                    scene,
                    scene_id,
                    "user",
                    content,
                    user_id=ctx.user_id,
                    nickname=ctx.nickname,
                    sender_role=ctx.sender.role,
                )
                await self.bot.storage.add_chat_message(scene, scene_id, "assistant", reply.text)
            except Exception:  # pragma: no cover - 存历史失败不影响回复
                self.logger.exception("保存聊天历史失败")

            from .output import send_chat_reply
            await send_chat_reply(ctx, reply.text)
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


__all__ = ["ChatReply", "ChatService", "group_user_content", "speaker_label", "split_text"]
