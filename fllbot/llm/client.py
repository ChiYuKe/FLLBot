"""OpenAI 兼容聊天客户端：多 provider、按场景/任务路由、返回用量。

保持旧接口不变（``chat`` / ``chat_stream`` / ``close`` / ``endpoint``），
新增 ``chat_result`` 返回文本 + usage，供上层记录 token 与延迟。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import LLMConfig, LLMProviderConfig
from ..tracing import record, span
from .budget import fit_messages, request_tokens, wire_messages
from .tool_protocol import ToolProtocolError, contains_dsml
from .tool_protocol import parse_dsml_tool_calls as _parse_dsml_tool_calls

DEFAULT_CONTEXT_WINDOW = 8192

# 网络异常、429 和临时 5xx 的有限重试间隔；其他 HTTP 错误直接返回。
TRANSIENT_RETRY_DELAYS: tuple[float, ...] = (0.6, 1.5)


class LLMError(RuntimeError):
    """调用大模型失败。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class EmptyResponseError(LLMError):
    """Upstream completed without a usable answer; safe to retry once."""


class ToolsUnavailableError(LLMError):
    """The model requested tools during a request that exposed none."""


@dataclass
class ChatResult:
    """一次模型调用的结果（含用量）。"""

    text: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    """缺失服务商用量时，使用 UTF-8 字节数作保守文本估算。"""
    stripped = (text or "").strip()
    if not stripped:
        return 0
    return len(stripped.encode("utf-8"))


class LLMClient:
    """按配置在多个 OpenAI 兼容 provider 之间路由。"""

    def __init__(
        self,
        config_provider: Callable[[], LLMConfig],
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config_provider = config_provider
        self._http = http_client
        self._owns_client = http_client is None
        self.logger = logging.getLogger("fllbot.llm")
        self._catalogs: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
        self._capacity = asyncio.Condition()
        self._active: dict[str, int] = {}

    @property
    def config(self) -> LLMConfig:
        return self._config_provider()

    @property
    def default_name(self) -> str:
        return self.config.route()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        return self._http

    async def close(self) -> None:
        if self._http is not None and self._owns_client:
            await self._http.aclose()
            self._http = None

    # -- 路由 ------------------------------------------------------------- #
    @staticmethod
    def endpoint(base_url: str) -> str:
        base = (base_url or "").rstrip("/")
        if not base:
            raise LLMError("未配置 base_url")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def resolve(
        self,
        *,
        provider: str | None = None,
        scene: str | None = None,
        task: str | None = None,
    ) -> tuple[str, LLMProviderConfig]:
        config = self.config
        name = provider or config.route(scene=scene, task=task)
        return config.resolve(name)

    def providers_status(self) -> list[dict[str, Any]]:
        """供管理 API 展示各 provider 的配置状态（不返回密钥）。"""
        config = self.config
        result: list[dict[str, Any]] = []
        for name in config.provider_names():
            try:
                resolved, provider = config.resolve(name)
            except Exception:  # pragma: no cover - resolve 已做兜底
                continue
            key = self._api_key(provider)
            result.append(
                {
                    "name": resolved,
                    "model": provider.model,
                    "base_url": provider.base_url,
                    "configured": bool(key),
                    "stream": not provider.no_stream,
                    "api_key_env": provider.api_key_env,
                    "is_default": resolved == config.route(),
                    **{
                        key: getattr(provider, key)
                        for key in (
                            "context_window",
                            "supports_tools",
                            "supports_images",
                            "supports_temperature",
                            "token_parameter",
                            "image_token_budget",
                            "max_concurrency",
                            "max_tokens",
                            "input_price",
                            "output_price",
                        )
                    },
                }
            )
        return result

    def _api_key(self, provider: LLMProviderConfig) -> str:
        if provider.api_key_env:
            value = os.getenv(provider.api_key_env, "").strip()
            if value:
                return value
        return provider.api_key.strip()

    def _headers(self, provider: LLMProviderConfig) -> dict[str, str]:
        key = self._api_key(provider)
        if not key:
            hint = f"（环境变量 {provider.api_key_env}）" if provider.api_key_env else ""
            raise LLMError(f"未配置 API Key{hint}")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _payload(
        self,
        provider: LLMProviderConfig,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int | None,
        temperature: float | None,
        stream: bool,
        tools: Sequence[dict[str, Any]] | None = None,
        unbounded_output: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "model": provider.model,
            "messages": wire_messages(messages),
            "stream": stream,
        }
        if not unbounded_output:
            payload[provider.token_parameter] = int(
                max_tokens if max_tokens is not None else provider.max_tokens or 4000
            )
        if provider.supports_temperature:
            payload["temperature"] = float(
                temperature
                if temperature is not None
                else provider.temperature
                if provider.temperature is not None
                else 0.8
            )
        if tools:
            payload["tools"] = [dict(item) for item in tools]
            payload["tool_choice"] = "auto"
        return payload

    async def _post_with_retry(
        self,
        url: str,
        config: LLMProviderConfig,
        payload: dict[str, Any],
        timeout: float | None,
    ) -> httpx.Response:
        """发送请求；连接失败、超时等传输层错误按 ``TRANSIENT_RETRY_DELAYS`` 重试。"""
        attempts = len(TRANSIENT_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                response = await self._client().post(
                    url,
                    headers=self._headers(config),
                    json=payload,
                    timeout=timeout or config.timeout or 60.0,
                )
                if response.status_code not in {429, 500, 502, 503, 504} or attempt + 1 >= attempts:
                    return response
                delay = TRANSIENT_RETRY_DELAYS[attempt]
                retry_after = response.headers.get("retry-after", "")
                try:
                    delay = max(delay, float(retry_after)) if retry_after else delay
                except ValueError:
                    from email.utils import parsedate_to_datetime

                    try:
                        delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (TypeError, ValueError, OverflowError):
                        pass
                # Do not retry sooner than requested when the wait exceeds our bounded retry policy.
                if delay > 30:
                    return response
                await response.aclose()
                record("model", "retry_scheduled", "skipped")
                await asyncio.sleep(max(0, delay))
            except httpx.TransportError as exc:
                if attempt + 1 >= attempts:
                    raise
                self.logger.warning(
                    "模型请求网络异常，%.1f 秒后重试（%d/%d）：%s",
                    TRANSIENT_RETRY_DELAYS[attempt],
                    attempt + 1,
                    attempts - 1,
                    type(exc).__name__,
                )
                record("model", "retry_scheduled", "skipped")
                await asyncio.sleep(TRANSIENT_RETRY_DELAYS[attempt])
        raise AssertionError("重试循环不应到达这里")

    # -- 调用 ------------------------------------------------------------- #
    @asynccontextmanager
    async def capacity(self, name: str):
        async with self._capacity:
            await self._capacity.wait_for(
                lambda: self._active.get(name, 0) < self.config.resolve(name)[1].max_concurrency
            )
            self._active[name] = self._active.get(name, 0) + 1
        try:
            yield
        finally:
            async with self._capacity:
                self._active[name] -= 1
                self._capacity.notify_all()

    async def prepare_messages(self, messages, *, provider=None, scene=None, tools=None, max_tokens=None):
        name, config = self.resolve(provider=provider, scene=scene)
        if tools and not config.supports_tools:
            raise LLMError("该模型未启用工具调用能力，请调整模型能力或该群工具设置")
        if not config.supports_images and any(
            isinstance(item.get("content"), list) and any(part.get("type") == "image_url" for part in item["content"])
            for item in messages
        ):
            raise LLMError("该模型未启用识图能力，请关闭图片输入或选择支持识图的模型")
        window = await self._effective_context_window(name, config)
        try:
            return fit_messages(
                list(messages),
                list(tools) if tools else None,
                window,
                max_tokens if max_tokens is not None else config.max_tokens or 4000,
                config.image_token_budget,
            )
        except ValueError as exc:
            raise LLMError(str(exc)) from exc

    async def _effective_context_window(self, name: str, config: LLMProviderConfig) -> int:
        return (
            config.context_window
            or (await self.context_window(provider=name) if self.config.context_mode == "model" else None)
            or DEFAULT_CONTEXT_WINDOW
        )

    async def chat_result(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        allow_fallback: bool = True,
        **kwargs: Any,
    ) -> ChatResult:
        name, _ = self.resolve(provider=kwargs.get("provider"), scene=kwargs.get("scene"), task=kwargs.get("task"))
        names = [name]
        if allow_fallback:
            names += [
                item for item in self.config.fallback_providers if item != name and item in self.config.provider_names()
            ]
        for index, candidate in enumerate(dict.fromkeys(names)):
            try:
                with span("model", candidate):
                    async with self.capacity(candidate):
                        try:
                            return await self._chat_result_once(messages, **{**kwargs, "provider": candidate})
                        except EmptyResponseError:
                            self.logger.warning("模型服务 %s 返回空结果，重试一次", candidate)
                            record("model", "retry_scheduled", "skipped")
                            return await self._chat_result_once(messages, **{**kwargs, "provider": candidate})
            except LLMError as exc:
                if index + 1 >= len(set(names)) or (
                    exc.status is not None and exc.status not in {429, 500, 502, 503, 504}
                ):
                    raise
                # Local capability/budget errors do not trigger a network failover.
                if (
                    exc.status is None
                    and not isinstance(exc, EmptyResponseError)
                    and not str(exc).startswith("网络请求失败")
                ):
                    raise
                self.logger.warning("模型服务 %s 暂不可用，尝试备用服务", candidate)
                record("model", "fallback_selected", component=candidate)
        raise AssertionError("没有可用模型服务")

    async def _chat_result_once(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        provider: str | None = None,
        scene: str | None = None,
        task: str | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        unbounded_output: bool = False,
    ) -> ChatResult:
        """调用模型并返回文本与用量。"""
        name, config = self.resolve(provider=provider, scene=scene, task=task)
        context_window = await self._effective_context_window(name, config)
        messages = await self.prepare_messages(messages, provider=name, tools=tools, max_tokens=max_tokens)
        url = self.endpoint(config.base_url)
        payload = self._payload(
            config,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
            tools=tools,
            unbounded_output=unbounded_output,
        )
        input_estimate = request_tokens(list(messages), list(tools) if tools else None, config.image_token_budget)
        output_limit = None if unbounded_output else int(payload[config.token_parameter])
        self.logger.info(
            "模型请求开始：provider=%s model=%s 输入上下文估算=%s/%s tokens 输出上限=%s 消息数=%s 工具数=%s",
            name,
            config.model,
            input_estimate,
            context_window,
            "未指定（服务商默认）" if output_limit is None else f"{output_limit} tokens",
            len(messages),
            len(tools or ()),
            extra={
                "event": "model_call_started",
                "provider": name,
                "model": config.model,
                "input_estimate_tokens": input_estimate,
                "context_window_tokens": context_window,
                "output_limit_tokens": output_limit,
                "output_limit_mode": "provider_default" if unbounded_output else "explicit",
                "message_count": len(messages),
                "tool_count": len(tools or ()),
            },
        )
        started = time.perf_counter()
        try:
            response = await self._post_with_retry(url, config, payload, timeout)
        except httpx.HTTPError as exc:
            raise LLMError(f"网络请求失败（{type(exc).__name__}）") from None

        if response.status_code >= 400:
            raise LLMError(_error_text(response), status=response.status_code)
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise LLMError("返回内容不是 JSON") from exc

        if not isinstance(data, dict):
            raise LLMError("返回内容必须是 JSON 对象")
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("返回中没有 choices")
        if not isinstance(choices, list) or not isinstance(choices[0], dict):
            raise LLMError("返回内容 choices 格式错误")
        message = choices[0].get("message") or {}
        if not isinstance(message, dict):
            raise LLMError("返回内容 message 格式错误")
        content = message.get("content")
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content.strip()
        else:
            raise LLMError("返回内容 message.content 格式错误")
        raw_tool_calls = message.get("tool_calls") or []
        if not isinstance(raw_tool_calls, list):
            raise LLMError("返回内容 message.tool_calls 格式错误")
        finish_reason = choices[0].get("finish_reason") or "unknown"
        # A length-truncated call is reported to the model layer as an incomplete call,
        # not as an unsolicited tool request, so the turn can still answer with a notice.
        if not tools and (raw_tool_calls or contains_dsml(text)) and finish_reason != "length":
            raise ToolsUnavailableError("模型在无可用工具的收尾阶段仍请求调用工具，本次未执行")
        if text:
            try:
                text, recovered = _parse_dsml_tool_calls(text, tools)
                if not raw_tool_calls:
                    raw_tool_calls = recovered
            except ToolProtocolError as exc:
                raise LLMError(str(exc)) from exc
        if not text and not raw_tool_calls:
            reason = choices[0].get("finish_reason") or "unknown"
            if reason == "content_filter":
                raise LLMError("模型服务因内容过滤未返回回复（finish_reason=content_filter）")
            raise EmptyResponseError(
                f"模型服务返回空结果（provider={name}, finish_reason={reason}）；"
                + (
                    "输出预算已耗尽，请检查应用输出上限或服务商最大生成长度"
                    if reason == "length"
                    else "未收到正文或工具调用，请检查模型服务兼容性"
                )
            )
        tool_calls = [item for item in raw_tool_calls if isinstance(item, dict)]

        usage = data.get("usage") or {}
        usage = usage if isinstance(usage, dict) else {}
        prompt_tokens = _token_count(usage.get("prompt_tokens"))
        completion_tokens = _token_count(usage.get("completion_tokens"))
        prompt_estimated = prompt_tokens is None
        completion_estimated = completion_tokens is None
        estimated = prompt_estimated or completion_estimated
        if prompt_tokens is None:
            prompt_tokens = request_tokens(list(messages), list(tools) if tools else None, config.image_token_budget)
        if completion_tokens is None:
            completion_tokens = estimate_tokens(text) + (
                estimate_tokens(json.dumps(tool_calls, ensure_ascii=False)) if tool_calls else 0
            )

        model = str(data.get("model") or config.model)
        finish_reason = str(choices[0].get("finish_reason") or "")
        output_summary = "无应用层上限" if output_limit is None else f"{output_limit} tokens"
        self.logger.info(
            "模型请求完成：provider=%s model=%s 输入上下文=%s/%s tokens（%s）输出=%s/%s tokens（%s）总计=%s tokens finish_reason=%s 耗时=%.0fms",
            name,
            model,
            prompt_tokens,
            context_window,
            "估算" if prompt_estimated else "服务商实报",
            completion_tokens,
            output_summary,
            "估算" if completion_estimated else "服务商实报",
            prompt_tokens + completion_tokens,
            finish_reason or "未提供",
            (time.perf_counter() - started) * 1000,
            extra={
                "event": "model_call_completed",
                "provider": name,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "prompt_tokens_estimated": prompt_estimated,
                "completion_tokens": completion_tokens,
                "completion_tokens_estimated": completion_estimated,
                "total_tokens": prompt_tokens + completion_tokens,
                "context_window_tokens": context_window,
                "output_limit_tokens": output_limit,
                "output_limit_mode": "provider_default" if unbounded_output else "explicit",
                "finish_reason": finish_reason,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )

        return ChatResult(
            text=text,
            model=model,
            provider=name,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "estimated": int(estimated),
            },
            finish_reason=finish_reason,
            tool_calls=tool_calls,
        )

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        provider: str | None = None,
        scene: str | None = None,
        task: str | None = None,
    ) -> str:
        """一次性返回完整回复文本（兼容旧调用）。"""
        result = await self.chat_result(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            provider=provider,
            scene=scene,
            task=task,
        )
        return result.text

    async def chat_stream(
        self,
        messages: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        name, _ = self.resolve(provider=kwargs.get("provider"), scene=kwargs.get("scene"), task=kwargs.get("task"))
        # Never restart a stream after yielding text; it would duplicate partial replies.
        async with self.capacity(name):
            async for piece in self._chat_stream_once(messages, **{**kwargs, "provider": name}):
                yield piece

    async def _chat_stream_once(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        provider: str | None = None,
        scene: str | None = None,
        task: str | None = None,
    ) -> AsyncIterator[str]:
        """流式返回增量文本（P1 接入对话链路）。"""
        name, config = self.resolve(provider=provider, scene=scene, task=task)
        if config.no_stream:
            result = await self._chat_result_once(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                provider=name,
            )
            if result.text:
                yield result.text
            return

        messages = await self.prepare_messages(messages, provider=name, max_tokens=max_tokens)
        url = self.endpoint(config.base_url)
        payload = self._payload(config, messages, max_tokens=max_tokens, temperature=temperature, stream=True)
        try:
            async with self._client().stream(
                "POST",
                url,
                headers=self._headers(config),
                json=payload,
                timeout=timeout or config.timeout or 60.0,
            ) as response:
                if response.status_code >= 400:
                    raise LLMError(_error_text(response), status=response.status_code)
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
        except httpx.HTTPError as exc:
            raise LLMError(f"网络请求失败（{type(exc).__name__}）") from None

    async def list_models(self, provider: str | None = None) -> list[str]:
        return [item["id"] for item in await self.model_catalog(provider=provider)]

    async def model_catalog(
        self,
        *,
        provider: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        cached: bool = False,
    ) -> list[dict[str, Any]]:
        """Discover using draft credentials without saving them or returning secrets."""
        _, resolved = self.resolve(provider=provider)
        config = resolved.model_copy(
            update={
                "base_url": base_url.strip() if base_url is not None else resolved.base_url,
                **({"api_key": api_key.strip(), "api_key_env": ""} if api_key else {}),
            }
        )
        base = self.endpoint(config.base_url).removesuffix("/chat/completions")
        key = (base, self._api_key(config))
        previous = self._catalogs.get(key)
        if cached and previous and time.monotonic() - previous[0] < 300:
            return previous[1]
        try:
            response = await self._client().get(
                f"{base}/models",
                headers=self._headers(config),
                timeout=min(config.timeout or 20.0, 20.0),
            )
        except httpx.HTTPError as exc:
            raise LLMError("无法连接模型服务，请检查服务地址与网络") from exc
        if response.status_code >= 400:
            # Discovery errors must never echo upstream request credentials.
            raise LLMError(
                f"获取模型列表失败（HTTP {response.status_code}），请检查地址、密钥和权限", status=response.status_code
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError("模型列表接口未返回有效 JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise LLMError("模型列表格式错误：缺少 data 数组")
        result: dict[str, dict[str, Any]] = {}
        for item in data["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                continue
            window = None
            for metadata in (item, item.get("top_provider"), item.get("limits")):
                if not isinstance(metadata, dict):
                    continue
                for context_key in ("context_window", "context_length", "max_context_length", "max_input_tokens"):
                    value = metadata.get(context_key)
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                        window = value
                        break
                if window:
                    break
            result[item["id"]] = {"id": item["id"], "context_window": window}
        catalog = sorted(result.values(), key=lambda item: item["id"].lower())
        self._catalogs[key] = (time.monotonic(), catalog)
        return catalog

    async def context_window(self, *, scene: str | None = None, provider: str | None = None) -> int | None:
        name, config = self.resolve(scene=scene, provider=provider)
        if config.context_window:
            return config.context_window
        try:
            catalog = await self.model_catalog(provider=name, cached=True)
        except LLMError:
            base = self.endpoint(config.base_url).removesuffix("/chat/completions")
            self._catalogs[(base, self._api_key(config))] = (time.monotonic(), [])
            return None
        return next((item["context_window"] for item in catalog if item["id"] == config.model), None)


def _error_text(response: httpx.Response) -> str:
    # Upstream error bodies may echo the complete prompt, tools or credentials.
    labels = {
        400: "模型服务拒绝请求，请检查模型参数",
        401: "模型服务身份验证失败，请检查密钥",
        403: "模型服务拒绝访问，请检查权限",
        404: "模型或接口不存在，请检查服务配置",
        429: "模型服务限流或额度不足",
    }
    return f"HTTP {response.status_code}: " + labels.get(response.status_code, "模型服务暂时不可用")


def _token_count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20:
        return int(value)
    return None


__all__ = ["ChatResult", "DEFAULT_CONTEXT_WINDOW", "LLMClient", "LLMError", "estimate_tokens"]
