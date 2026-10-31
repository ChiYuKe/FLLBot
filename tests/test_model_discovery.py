from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.config import LLMConfig
from fllbot.llm.client import LLMClient, LLMError
from helpers import build_bot, make_bot

AUTH = {"Authorization": "Bearer admin-token"}


async def test_catalog_draft_credentials_and_metadata() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [
            {"id": "z-model", "context_length": 65536},
            {"id": "a-model", "top_provider": {"context_length": 32768}},
            {"id": "unknown"}, {"id": ""}, {"name": "invalid"},
        ]})

    config = LLMConfig(base_url="https://saved.test/v1", api_key="saved-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(lambda: config, http_client=http)
        catalog = await client.model_catalog(base_url="https://draft.test/v1/chat/completions", api_key="draft-key")
    assert str(requests[0].url) == "https://draft.test/v1/models"
    assert requests[0].headers["authorization"] == "Bearer draft-key"
    assert catalog == [
        {"id": "a-model", "context_window": 32768},
        {"id": "unknown", "context_window": None},
        {"id": "z-model", "context_window": 65536},
    ]
    assert config.api_key == "saved-key"
    assert config.base_url == "https://saved.test/v1"


@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"error": {"message": "credential=secret-key"}}),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"data": {}}),
])
async def test_discovery_errors_do_not_echo_credentials(response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        client = LLMClient(lambda: LLMConfig(api_key="secret-key"), http_client=http)
        with pytest.raises(LLMError) as error:
            await client.model_catalog()
    assert "secret-key" not in str(error.value)


async def test_discovery_reuses_saved_env_key_and_keeps_id_list_compatibility(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_TEST_KEY", "env-test-key")
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    config = LLMConfig(providers={"default": {"api_key_env": "MODEL_TEST_KEY"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(lambda: config, http_client=http)
        assert await client.list_models() == ["model-a"]
        assert captured[0].headers["authorization"] == "Bearer env-test-key"
        assert await client.context_window() is None
        assert len(captured) == 1


def test_discovery_api_auth_and_does_not_save(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=False)
    before = bot.config.llm.model_dump()
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"data": [{"id": "chosen", "context_window": 16384}]})

    with TestClient(create_app(bot)) as api:
        bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert api.post("/api/llm/models/discover", json={}).status_code == 401
        response = api.post("/api/llm/models/discover", headers=AUTH, json={
            "base_url": "https://draft.test/v1", "api_key": "draft-key",
        })
        assert response.status_code == 200
        assert response.json()["models"] == [{"id": "chosen", "context_window": 16384}]
        assert "draft-key" not in response.text
        assert bot.config.llm.model_dump() == before
        assert len(captured) == 1


async def test_model_context_uses_window_instead_of_legacy_message_cap(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, builtin=False, llm={
        "context_mode": "model", "api_key": "test-key", "model": "test-model",
        "max_context_messages": 0, "max_context_chars": 1, "max_tokens": 200,
    })
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"id": "test-model", "context_window": 4096}]})

    try:
        bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        for index in range(20):
            await bot.storage.add_chat_message("private", 1, "user", str(index) + "x" * 600)
        messages = await bot.chat.build_messages("private", 1, "current")
        assert 2 < len(messages) < 22
        assert messages[-1]["content"] == "current"
        assert messages[-2]["content"].startswith("19x")
        assert sum(len(item["content"].encode()) + 8 for item in messages) <= 4096 - 200
        await bot.chat.build_messages("private", 1, "second")
        assert len(calls) == 1  # reuse metadata without contacting the service each turn
        assert len(await bot.chat.build_messages("private", 1, "no history", include_history=False)) == 2
    finally:
        await bot.stop()
