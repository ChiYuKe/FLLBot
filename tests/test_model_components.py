"""Exercise context assembly and model requests without constructing an application."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from fllbot.config import AppConfig
from fllbot.conversation import turn_scope
from fllbot.llm.client import LLMClient, LLMError
from fllbot.llm.context_assembly import ContextAssembler
from fllbot.llm.context_builder import ContextBuilder
from fllbot.llm.model_gateway import ModelGateway


@pytest.mark.parametrize("failed", [False, True])
async def test_context_to_gateway_uses_only_injected_dependencies(failed):
    config = AppConfig()
    config.llm.api_key = "fake-key"
    config.llm.context_window = 8192
    config.llm.context_mode = "custom"
    storage = SimpleNamespace(record_llm_usage=AsyncMock())
    history = AsyncMock(return_value=[SimpleNamespace(role="user", content="earlier question")])
    persona = SimpleNamespace(render=lambda **values: "persona constraints")

    def handle(request):
        if failed:
            return httpx.Response(401, json={"error": {"message": "denied"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = LLMClient(lambda: config.llm, http_client=http)

        def route(*args):
            return "default"

        gateway = ModelGateway(
            configuration=lambda: config.llm,
            client=lambda: client,
            storage=storage,
            provider_name=route,
            execution_scope=lambda *args: nullcontext(),
        )
        assembler = ContextAssembler(
            configuration=lambda: config,
            personas=lambda: SimpleNamespace(get=lambda name: persona),
            persona_name=route,
            provider_name=route,
            policy=lambda group: {},
            nickname=lambda account: "Bot",
            client=lambda: client,
            history=history,
            memory=None,
        )
        builder = ContextBuilder(
            assembler.assemble, lambda: config, assembler.resolve_provider, gateway.describe_budget
        )
        snapshot = await builder.build("private", 123, "current request")
        assert snapshot.messages() == [
            {"role": "system", "content": "persona constraints"},
            {"role": "user", "content": "earlier question"},
            {"role": "user", "content": "current request"},
        ]
        history.assert_awaited_once()
        assert snapshot.budget()["providers"][0]["window"] == 8192
        with turn_scope("standalone-turn"):
            if failed:
                with pytest.raises(LLMError):
                    await gateway.complete(snapshot.messages(), scene="private", scene_id=123)
            else:
                reply = await gateway.complete(snapshot.messages(), scene="private", scene_id=123)
                assert reply.text == "answer" and reply.summary()["usage"]["prompt_tokens"] == 10
        assert gateway.requests == 1 and gateway.failures == int(failed)
        record = storage.record_llm_usage.await_args.kwargs
        assert record["turn_id"] == "standalone-turn" and record["ok"] is not failed
        assert "content" not in record and "messages" not in record
