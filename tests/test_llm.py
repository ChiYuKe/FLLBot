"""LLM 对话测试（使用 httpx MockTransport，不访问网络）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.config import LLMConfig
from fllbot.llm.chat import split_text
from fllbot.llm.client import LLMClient
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, build_bot, group_message, make_bot

AUTH = {"Authorization": "Bearer admin-token"}

LLM_CONFIG = {
    "enabled": True,
    "api_key": "test-key",
    "base_url": "http://mock.local",
    "model": "test-model",
    "cooldown_seconds": 0,
    "max_context_messages": 6,
    "max_context_chars": 2000,
    "chunk_size": 500,
}


def make_transport(reply_text: str, captured: list[dict[str, Any]], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if status >= 400:
            return httpx.Response(status, json={"error": {"message": "mock failure"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply_text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    return httpx.MockTransport(handler)


@pytest.fixture
async def llm_bot(tmp_path: Path):
    bot = await make_bot(tmp_path, builtin=False, llm=LLM_CONFIG)
    try:
        yield bot
    finally:
        await bot.chat.client.close()
        await bot.stop()


def patch_client(bot, transport: httpx.MockTransport) -> None:
    http = httpx.AsyncClient(transport=transport)
    bot.chat.client = LLMClient(lambda: bot.config.llm, http_client=http)


async def test_respond_saves_history(llm_bot) -> None:
    captured: list[dict[str, Any]] = []
    patch_client(llm_bot, make_transport("你好呀", captured))

    conn = FakeConnection()
    ctx = Context(bot=llm_bot, connection=conn, event=parse_event(group_message("早上好", at=10001)))  # type: ignore[arg-type]
    reply = await llm_bot.chat.respond(ctx, "早上好")

    assert reply is not None
    assert reply.text == "你好呀"
    assert conn.texts() == ["你好呀"]
    assert captured[0]["model"] == "test-model"
    assert captured[0]["messages"][0]["role"] == "system"
    assert captured[0]["messages"][-1] == {"role": "user", "content": "早上好"}

    rows = await llm_bot.storage.recent_chat("group", 20002)
    assert rows == [("user", "早上好"), ("assistant", "你好呀")]


async def test_second_turn_includes_history(llm_bot) -> None:
    captured: list[dict[str, Any]] = []
    patch_client(llm_bot, make_transport("嗯嗯", captured))

    for text in ("第一句", "第二句"):
        conn = FakeConnection()
        ctx = Context(bot=llm_bot, connection=conn, event=parse_event(group_message(text, at=10001)))  # type: ignore[arg-type]
        await llm_bot.chat.respond(ctx, text)

    second = captured[1]["messages"]
    roles = [item["role"] for item in second]
    assert roles == ["system", "user", "assistant", "user"]
    assert second[1]["content"] == "第一句"


async def test_persona_is_rendered(llm_bot) -> None:
    captured: list[dict[str, Any]] = []
    patch_client(llm_bot, make_transport("ok", captured))
    conn = FakeConnection()
    ctx = Context(bot=llm_bot, connection=conn, event=parse_event(group_message("hi", at=10001)))  # type: ignore[arg-type]
    await llm_bot.chat.respond(ctx, "hi")
    system = captured[0]["messages"][0]["content"]
    assert "{bot_name}" not in system
    assert "QQ" in system or "群" in system


async def test_error_returns_none(llm_bot) -> None:
    captured: list[dict[str, Any]] = []
    patch_client(llm_bot, make_transport("", captured, status=500))
    conn = FakeConnection()
    ctx = Context(bot=llm_bot, connection=conn, event=parse_event(group_message("hi", at=10001)))  # type: ignore[arg-type]

    assert await llm_bot.chat.respond(ctx, "hi") is None
    assert llm_bot.chat.failures == 1
    assert llm_bot.chat.last_error is not None
    assert conn.sent == []


async def test_long_reply_is_chunked(llm_bot) -> None:
    llm_bot.config_store.patch({"llm": {"chunk_size": 10}})
    long_text = "第一段内容很长。第二段内容也很长。第三段内容同样很长。"
    captured: list[dict[str, Any]] = []
    patch_client(llm_bot, make_transport(long_text, captured))

    conn = FakeConnection()
    ctx = Context(bot=llm_bot, connection=conn, event=parse_event(group_message("hi", at=10001)))  # type: ignore[arg-type]
    await llm_bot.chat.respond(ctx, "hi")

    assert len(conn.sent) > 1
    assert "".join(conn.texts()) == long_text


async def test_chat_disabled(llm_bot) -> None:
    llm_bot.config_store.patch({"llm": {"enabled": False}})
    conn = FakeConnection()
    ctx = Context(bot=llm_bot, connection=conn, event=parse_event(group_message("hi", at=10001)))  # type: ignore[arg-type]
    assert await llm_bot.chat.respond(ctx, "hi") is None


async def test_group_llm_disabled_by_config(llm_bot) -> None:
    await llm_bot.permissions.set_group_config(20002, {"llm": {"enabled": False}})
    conn = FakeConnection()
    ctx = Context(bot=llm_bot, connection=conn, event=parse_event(group_message("hi", at=10001)))  # type: ignore[arg-type]
    assert await llm_bot.chat.respond(ctx, "hi") is None


def test_split_text() -> None:
    assert split_text("", 10) == []
    assert split_text("short", 10) == ["short"]
    chunks = split_text("a" * 25, 10)
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert "".join(chunks) == "a" * 25


def test_endpoint_normalization() -> None:
    assert LLMClient.endpoint("https://api.deepseek.com") == "https://api.deepseek.com/v1/chat/completions"
    assert LLMClient.endpoint("https://api.openai.com/v1") == "https://api.openai.com/v1/chat/completions"
    assert LLMClient.endpoint("http://x/chat/completions") == "http://x/chat/completions"


def test_legacy_config_maps_to_default_provider() -> None:
    config = LLMConfig.model_validate({"enabled": True, "api_key": "k", "model": "m1", "base_url": "http://x"})
    assert config.provider_names() == ["default"]
    name, provider = config.resolve()
    assert name == "default"
    assert provider.model == "m1"
    assert provider.api_key == "k"
    assert provider.base_url == "http://x"


def test_provider_routing_and_inheritance() -> None:
    config = LLMConfig.model_validate(
        {
            "enabled": True,
            "api_key": "legacy-key",
            "model": "legacy-model",
            "default": "deepseek",
            "providers": {
                "deepseek": {"base_url": "http://a", "model": "chat", "api_key_env": "DEEPSEEK_KEY"},
                "local": {"base_url": "http://b", "model": "qwen", "api_key": "local-key"},
            },
            "routes": {"summary": "local", "group": "local"},
        }
    )
    assert config.route(task="summary") == "local"
    assert config.route(scene="group") == "local"
    assert config.route(scene="private") == "deepseek"

    name, provider = config.resolve("deepseek")
    assert name == "deepseek"
    assert provider.model == "chat"
    assert provider.api_key == "legacy-key"  # 未单独配置时继承 [llm]
    assert provider.api_key_env == "DEEPSEEK_KEY"

    # 名字不存在时退回第一个 provider
    name, provider = config.resolve("missing")
    assert name == "deepseek"


def test_providers_status(monkeypatch: pytest.MonkeyPatch) -> None:
    config = LLMConfig.model_validate(
        {
            "enabled": True,
            "default": "a",
            "providers": {
                "a": {"base_url": "http://a", "model": "m-a", "api_key": "key-a"},
                "b": {"base_url": "http://b", "model": "m-b", "api_key_env": "FLLBOT_TEST_LLM_KEY"},
            },
        }
    )
    client = LLMClient(lambda: config)
    status = {item["name"]: item for item in client.providers_status()}
    assert status["a"]["configured"] is True
    assert status["a"]["is_default"] is True

    monkeypatch.delenv("FLLBOT_TEST_LLM_KEY", raising=False)
    status = {item["name"]: item for item in client.providers_status()}
    assert status["b"]["configured"] is False

    monkeypatch.setenv("FLLBOT_TEST_LLM_KEY", "env-key")
    status = {item["name"]: item for item in client.providers_status()}
    assert status["b"]["configured"] is True


async def test_usage_is_recorded(llm_bot) -> None:
    captured: list[dict[str, Any]] = []
    patch_client(llm_bot, make_transport("用量测试", captured))
    conn = FakeConnection()
    ctx = Context(bot=llm_bot, connection=conn, event=parse_event(group_message("hi", at=10001)))  # type: ignore[arg-type]
    await llm_bot.chat.respond(ctx, "hi")

    summary = await llm_bot.storage.llm_usage_summary(0)
    assert summary["requests"] == 1
    assert summary["total_tokens"] == 15
    assert summary["by_model"][0]["model"] == "test-model"


def test_usage_api_endpoint(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=False, llm=LLM_CONFIG)
    with TestClient(create_app(bot)) as client:
        http = httpx.AsyncClient(transport=make_transport("ok", []))
        bot.chat.client = LLMClient(lambda: bot.config.llm, http_client=http)

        providers = client.get("/api/llm/providers", headers=AUTH).json()
        assert providers[0]["name"] == "default"

        response = client.post("/api/llm/test", headers=AUTH, json={"prompt": "hi"})
        assert response.status_code == 200
        assert response.json()["usage"]["total_tokens"] == 15

        usage = client.get("/api/llm/usage?range=all", headers=AUTH).json()
        assert usage["requests"] == 1
        assert usage["total_tokens"] == 15
        assert usage["by_model"][0]["model"] == "test-model"
