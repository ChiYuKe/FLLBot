import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from fllbot.config import LLMConfig
from fllbot.llm.agent_runtime import AgentRuntime
from fllbot.llm.budget import request_tokens
from fllbot.llm.client import ChatResult, LLMClient, _error_text
from fllbot.llm.model_gateway import ChatReply
from helpers import make_bot


async def test_reasoning_survives_client_gateway_and_repeated_tool_requests_without_storage(tmp_path, caplog):
    bot = await make_bot(
        tmp_path,
        builtin=False,
        llm={
            "enabled": True,
            "base_url": "https://model.invalid",
            "api_key": "test-key",
            "model": "thinking-model",
            "context_window": 32000,
            "max_tokens": 1000,
        },
    )
    calls = []
    secrets = ["opaque-protocol-state-one", "opaque-protocol-state-two"]

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assistants = [item for item in payload["messages"] if item["role"] == "assistant"]
        for index, item in enumerate(assistants):
            assert item["reasoning_content"] == secrets[index]
        if len(calls) <= 2:
            message = {
                "role": "assistant",
                "content": None,
                "reasoning_content": secrets[len(calls) - 1],
                "tool_calls": [
                    {"id": f"call-{len(calls)}", "type": "function", "function": {"name": "read", "arguments": "{}"}}
                ],
            }
        else:
            message = {"role": "assistant", "content": "完成"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bot.chat.client = LLMClient(lambda: bot.config.llm, http_client=http)
    execute = AsyncMock(return_value=('{"source":"verified"}', False))
    try:
        result = await AgentRuntime(bot.chat.complete).run(
            [{"role": "user", "content": "读取资料再回答"}],
            scene="group",
            scene_id=20002,
            ctx=SimpleNamespace(data_current=True, self_id=10001, tool_call_id=""),
            agent=True,
            tools=[{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}],
            registry={"read": None},
            execute=execute,
        )
        assert result.text == "完成" and execute.await_count == 2 and len(calls) == 3
        assert not any(secret in str(result.response.summary()) for secret in secrets)
        rows = await (await bot.storage.db.execute("SELECT error FROM llm_usage")).fetchall()
        assert len(rows) == 3
        dumped = "\n".join([line async for line in bot.storage.db.iterdump()])
        assert not any(secret in dumped for secret in secrets)
        assert not any(secret in caplog.text for secret in secrets)
    finally:
        await http.aclose()
        await bot.stop()


async def test_pending_user_input_preserves_reasoning_even_without_tool_call():
    pending = iter([True, False])
    complete = AsyncMock(
        side_effect=[
            SimpleNamespace(text="first draft", tool_calls=[], reasoning_content="opaque-state"),
            SimpleNamespace(text="final", tool_calls=[]),
        ]
    )
    messages = [{"role": "user", "content": "question"}]
    result = await AgentRuntime(complete).run(
        messages,
        scene="group",
        scene_id=1,
        ctx=SimpleNamespace(data_current=True, self_id=11),
        agent=True,
        pending=lambda: next(pending),
    )
    assert result.text == "final"
    assert messages[-1]["reasoning_content"] == "opaque-state"


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"error": {"message": "Missing reasoning_content. PRIVATE-PROMPT sk-secret"}}, "reasoning_content"),
        ({"error": {"message": "max_tokens out of range: PRIVATE-PROMPT"}}, "输出上限"),
        ({"error": {"message": "Invalid tool_call_id PRIVATE-PROMPT"}}, "消息格式"),
        ({"error": {"message": "PRIVATE-PROMPT sk-secret"}}, "检查模型参数"),
        (["PRIVATE-PROMPT"], "检查模型参数"),
    ],
)
def test_error_labels_are_actionable_without_echoing_response_bodies(body, expected):
    message = _error_text(httpx.Response(400, json=body))
    assert expected in message
    assert "PRIVATE-PROMPT" not in message and "sk-secret" not in message


def test_reasoning_is_hidden_from_repr_and_reply_summary_but_counted_in_wire_budget():
    state = "opaque-state"
    result = ChatResult(text="answer", model="model", provider="default", reasoning_content=state)
    reply = ChatReply(text="answer", model="model", persona="", reasoning_content=state)
    assert state not in repr(result) and state not in repr(reply) and state not in str(reply.summary())
    messages = [{"role": "assistant", "content": "answer"}]
    before = request_tokens(messages)
    messages[0]["reasoning_content"] = state * 100
    assert request_tokens(messages) > before + 1000


async def test_estimated_completion_usage_includes_reasoning_without_exposing_it():
    state = "opaque-state" * 100
    payload = {"choices": [{"message": {"content": "OK", "reasoning_content": state}}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        client = LLMClient(lambda: LLMConfig(api_key="test", context_window=8192), http_client=http)
        result = await client.chat_result([{"role": "user", "content": "question"}])
    assert result.usage["completion_tokens"] >= len(state)
    assert state not in str(result.usage)
