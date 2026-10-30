"""OpenAI 兼容聊天客户端：多 provider、按场景/任务路由、返回用量。

保持旧接口不变（``chat`` / ``chat_stream`` / ``close`` / ``endpoint``），
新增 ``chat_result`` 返回文本 + usage，供上层记录 token 与延迟。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import LLMConfig, LLMProviderConfig


class LLMError(RuntimeError):
    """调用大模型失败。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


@dataclass
class ChatResult:
    """一次模型调用的结果（含用量）。"""

    text: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""


def estimate_tokens(text: str) -> int:
    """粗略估算 token：中文约 1 字 1 token，英文约 2 字符 1 token，取折中。"""
    stripped = (text or "").strip()
    if not stripped:
        return 0
    return max(1, (len(stripped) + 1) // 2)


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
    ) -> dict[str, Any]:
        return {
            "model": provider.model,
            "messages": [dict(item) for item in messages],
            "max_tokens": int(max_tokens if max_tokens is not None else provider.max_tokens or 800),
            "temperature": float(
                temperature if temperature is not None else provider.temperature if provider.temperature is not None else 0.8
            ),
            "stream": stream,
        }

    # -- 调用 ------------------------------------------------------------- #
    async def chat_result(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        provider: str | None = None,
        scene: str | None = None,
        task: str | None = None,
    ) -> ChatResult:
        """调用模型并返回文本与用量。"""
        name, config = self.resolve(provider=provider, scene=scene, task=task)
        url = self.endpoint(config.base_url)
        payload = self._payload(config, messages, max_tokens=max_tokens, temperature=temperature, stream=False)
        try:
            response = await self._client().post(
                url,
                headers=self._headers(config),
                json=payload,
                timeout=timeout or config.timeout or 60.0,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"网络请求失败: {exc}") from exc

        if response.status_code >= 400:
            raise LLMError(_error_text(response), status=response.status_code)
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise LLMError("返回内容不是 JSON") from exc

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"返回中没有 choices: {str(data)[:200]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMError("返回内容缺少 message.content")
        text = content.strip()

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if prompt_tokens <= 0:
            prompt_tokens = sum(estimate_tokens(str(item.get("content") or "")) for item in messages)
        if completion_tokens <= 0:
            completion_tokens = estimate_tokens(text)

        return ChatResult(
            text=text,
            model=str(data.get("model") or config.model),
            provider=name,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            finish_reason=str(choices[0].get("finish_reason") or ""),
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
            result = await self.chat_result(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                provider=name,
            )
            if result.text:
                yield result.text
            return

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
                    body = await response.aread()
                    raise LLMError(body.decode("utf-8", "replace")[:300], status=response.status_code)
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
            raise LLMError(f"网络请求失败: {exc}") from exc

    async def list_models(self, provider: str | None = None) -> list[str]:
        name, config = self.resolve(provider=provider)
        base = config.base_url.rstrip("/")
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        response = await self._client().get(
            url,
            headers=self._headers(config),
            timeout=config.timeout or 60.0,
        )
        if response.status_code >= 400:
            raise LLMError(_error_text(response), status=response.status_code)
        data = response.json()
        return [item.get("id", "") for item in data.get("data", [])]


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f"{response.status_code}: {error['message']}"
    except (json.JSONDecodeError, ValueError):
        pass
    return f"{response.status_code}: {response.text[:300]}"


__all__ = ["ChatResult", "LLMClient", "LLMError", "estimate_tokens"]
