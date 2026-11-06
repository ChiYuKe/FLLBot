"""Model requests, quota reservations and usage facts; no conversation projection."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..conversation import ConversationLockPool, current_turn_id
from ..tracing import record, span
from .budget import request_tokens
from .client import DEFAULT_CONTEXT_WINDOW, LLMError


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
    finish_reason: str = ""
    reasoning_content: str | None = field(default=None, repr=False)

    def summary(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "persona": self.persona,
            "model": self.model,
            "provider": self.provider,
            "usage": dict(self.usage),
            "latency_ms": round(self.latency_ms, 1),
            "finish_reason": self.finish_reason,
            "truncated": self.finish_reason == "length",
        }


class ModelGateway:
    def __init__(self, *, configuration, client, storage, provider_name, execution_scope) -> None:
        self.configuration = configuration
        self.client_provider = client
        self.storage = storage
        self.provider_name = provider_name
        self.execution_scope = execution_scope
        self._quota_pool = ConversationLockPool()
        self.logger = logging.getLogger("fllbot.llm")
        self.requests = 0
        self.failures = 0
        self.last_error: str | None = None
        self.last_latency_ms = 0.0

    @property
    def config(self):
        return self.configuration()

    @property
    def client(self):
        return self.client_provider()

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
        with self.execution_scope(scene or "test", scene_id, self_id):
            with span("model", provider or "route"):
                return await self._complete_with_quota(
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
        options = dict(
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
        if scene != "group" or not (self.config.group_daily_token_limit or self.config.group_daily_cost_limit):
            return await self._complete(messages, **options)
        # Check and record within the same group lock, including concurrent admin/summary calls.
        async with self._quota_pool.lease(scene_id):
            name = provider or self.provider_name(scene, scene_id, task, self_id)
            _, resolved = self.client.resolve(provider=name)
            prepared = await self.client.prepare_messages(messages, provider=name, tools=tools, max_tokens=max_tokens)
            midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            usage = await self.storage.group_llm_usage(scene_id, midnight)
            candidates = [resolved]
            if allow_fallback:
                candidates += [
                    self.config.resolve(item)[1]
                    for item in self.config.fallback_providers
                    if item in self.config.provider_names()
                ]
            reservations = [
                (
                    request_tokens(prepared, tools, item.image_token_budget),
                    max_tokens if max_tokens is not None else item.max_tokens or 4000,
                    item,
                )
                for item in candidates
            ]
            if (
                self.config.group_daily_token_limit
                and usage["tokens"] + max(p + o for p, o, _ in reservations) > self.config.group_daily_token_limit
            ):
                raise LLMError("本群今日 Token 额度不足，已暂停本次模型调用")
            estimated_cost = max(
                (p * item.input_price + o * item.output_price) / 1_000_000 for p, o, item in reservations
            )
            if self.config.group_daily_cost_limit:
                if any(item.input_price <= 0 or item.output_price <= 0 for item in candidates):
                    raise LLMError("费用额度已启用，请为使用的模型及备用服务填写正数输入、输出单价")
                if usage["cost"] + estimated_cost > self.config.group_daily_cost_limit:
                    raise LLMError("本群今日费用额度不足，已暂停本次模型调用")
            return await self._complete(prepared, **options)

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
        started = time.perf_counter()
        self.requests += 1
        name = provider or self.provider_name(scene, scene_id, task, self_id)
        record("model", "provider_selected", component=name)
        try:
            result = await self.client.chat_result(
                messages,
                max_tokens=max_tokens,
                unbounded_output=unbounded_output,
                provider=name,
                scene=scene,
                task=task,
                tools=tools,
                allow_fallback=allow_fallback,
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
        record("model", "response_received", component=result.provider)
        self.last_latency_ms = latency
        self.last_error = None
        await self._record_usage(scene or "", scene_id, result.provider, result.model, result.usage, latency, ok=True)
        text = result.text
        tool_calls = result.tool_calls
        if result.finish_reason == "length":
            # Incomplete JSON must never reach a tool with external side effects.
            tool_calls = []
            text = (
                text + "\n\n[回复已达到长度上限，可发送“继续”接着回答。]"
                if not result.tool_calls
                else "工具调用参数未生成完整，本次未执行工具，请缩短请求或提高回复长度上限。"
            )
        return ChatReply(
            text=text,
            persona="",
            model=result.model,
            latency_ms=latency,
            provider=result.provider,
            usage=result.usage,
            tool_calls=tool_calls,
            finish_reason=result.finish_reason,
            reasoning_content=getattr(result, 'reasoning_content', None),
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
            if not model:
                try:
                    provider, resolved = self.config.resolve(provider or None)
                    model = resolved.model
                except KeyError:
                    pass
            await self.storage.record_llm_usage(
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
                usage_estimated=bool(usage.get("estimated", 0)),
                cached_prompt_tokens=usage.get('cached_prompt_tokens'),
                turn_id=current_turn_id(),
            )
        except Exception:  # pragma: no cover - 统计失败不影响对话
            self.logger.exception("记录 LLM 用量失败")

    async def describe_budget(self, provider: str, messages: list, tools: list) -> dict:
        names = dict.fromkeys([provider, *self.config.fallback_providers])
        providers = []
        for name in names:
            _, options = self.client.resolve(provider=name)
            window = (
                options.context_window
                or (await self.client.context_window(provider=name) if self.config.context_mode == "model" else None)
                or DEFAULT_CONTEXT_WINDOW
            )
            providers.append(
                {
                    "provider": name,
                    "model": options.model,
                    "window": window,
                    "output_reserve": options.max_tokens or 4000,
                    "image_reserve": options.image_token_budget,
                    "input_estimate": request_tokens(messages, tools, options.image_token_budget),
                }
            )
        return {"providers": providers, "estimator": "utf8_upper_bound", "final_check": "per_model_request"}
