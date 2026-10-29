"""极简 OpenAI 兼容聊天客户端（支持流式）。"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import httpx

from ..config import LLMConfig


class LLMError(RuntimeError):
    """调用大模型失败。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class LLMClient:
    """按当前配置调用 ``/chat/completions``。"""

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

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        return self._http

    async def close(self) -> None:
        if self._http is not None and self._owns_client:
            await self._http.aclose()
            self._http = None

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

    def _headers(self, config: LLMConfig) -> dict[str, str]:
        if not config.api_key:
            raise LLMError("未配置 llm.api_key")
        return {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}

    def _payload(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int | None,
        temperature: float | None,
        stream: bool,
    ) -> dict[str, Any]:
        config = self.config
        return {
            "model": config.model,
            "messages": [dict(item) for item in messages],
            "max_tokens": int(max_tokens if max_tokens is not None else config.max_tokens),
            "temperature": float(temperature if temperature is not None else config.temperature),
            "stream": stream,
        }

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> str:
        """一次性返回完整回复文本。"""
        config = self.config
        url = self.endpoint(config.base_url)
        payload = self._payload(messages, max_tokens=max_tokens, temperature=temperature, stream=False)
        try:
            response = await self._client().post(
                url,
                headers=self._headers(config),
                json=payload,
                timeout=timeout or config.timeout,
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
        return content.strip()

    async def chat_stream(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        """流式返回增量文本。"""
        config = self.config
        url = self.endpoint(config.base_url)
        payload = self._payload(messages, max_tokens=max_tokens, temperature=temperature, stream=True)
        try:
            async with self._client().stream(
                "POST",
                url,
                headers=self._headers(config),
                json=payload,
                timeout=timeout or config.timeout,
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

    async def list_models(self) -> list[str]:
        config = self.config
        base = config.base_url.rstrip("/")
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        response = await self._client().get(url, headers=self._headers(config), timeout=config.timeout)
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


__all__ = ["LLMClient", "LLMError"]
