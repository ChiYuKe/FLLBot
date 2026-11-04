from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.config import LLMConfig
from fllbot.llm.budget import fit_messages, request_tokens
from fllbot.llm.client import LLMClient, LLMError
from fllbot.onebot.models import parse_event
from helpers import FakeConnection, build_bot, group_message, make_bot

AUTH = {"Authorization": "Bearer admin-token"}
MESSAGES = [{"role": "system", "content": "rules"}, {"role": "user", "content": "current"}]


def response(text="ok", *, calls=None, finish="stop", tokens=15):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text, "tool_calls": calls or []}, "finish_reason": finish}],
            "usage": {"prompt_tokens": tokens - 5, "completion_tokens": 5},
        },
    )


def test_budget_preserves_current_tool_transaction_and_source():
    call = {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
    messages = [
        MESSAGES[0],
        {"role": "user", "content": "old" * 2000},
        {"role": "assistant", "content": "old answer"},
        MESSAGES[1],
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果" * 6000},
    ]
    fitted = fit_messages(messages, None, 2400, 200)
    assert request_tokens(fitted) + 200 <= 2400
    assert fitted[:2] == MESSAGES
    assert fitted[2]["tool_calls"] == [call]
    assert fitted[3]["tool_call_id"] == "c1"
    assert "截断" in fitted[3]["content"]
    assert messages[1]["content"] == "old" * 2000
    assert messages[-1]["content"] == "结果" * 6000


def test_budget_counts_schema_and_does_not_silently_truncate_user():
    with pytest.raises(ValueError, match="上下文预算"):
        fit_messages(MESSAGES, [{"description": "schema" * 1000}], 1000, 200)
    with pytest.raises(ValueError, match="上下文预算"):
        fit_messages([{"role": "user", "content": "问题" * 1000}], None, 1000, 200)


async def test_explicit_capabilities_and_output_parameter():
    captured = []
    config = LLMConfig(
        api_key="k",
        context_window=8192,
        supports_temperature=False,
        token_parameter="max_completion_tokens",
        max_tokens=200,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: captured.append(json.loads(req.content)) or response())
    ) as http:
        client = LLMClient(lambda: config, http_client=http)
        await client.chat_result(MESSAGES)
    assert captured[0]["max_completion_tokens"] == 200
    assert "max_tokens" not in captured[0] and "temperature" not in captured[0]


@pytest.mark.parametrize("kind", ["tools", "images"])
async def test_disabled_capability_rejected_before_request(kind):
    captured = []
    config = LLMConfig(api_key="k", context_window=8192, supports_tools=False, supports_images=False)
    messages = (
        MESSAGES
        if kind == "tools"
        else [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}},
                ],
            }
        ]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: captured.append(req) or response())) as http:
        client = LLMClient(lambda: config, http_client=http)
        with pytest.raises(LLMError, match="未启用"):
            await client.chat_result(messages, tools=[{"type": "function"}] if kind == "tools" else None)
    assert captured == []


async def test_http_retry_after_and_auth_errors(monkeypatch):
    monkeypatch.setattr("fllbot.llm.client.TRANSIENT_RETRY_DELAYS", (0, 0))
    calls, sleeps = [], []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("fllbot.llm.client.asyncio.sleep", sleep)

    def handler(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "2"}) if len(calls) == 1 else response()

    config = LLMConfig(api_key="k", context_window=8192)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(lambda: config, http_client=http)
        assert (await client.chat_result(MESSAGES)).text == "ok"
    assert len(calls) == 2 and sleeps == [2]
    calls.clear()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(401))
    ) as http:
        client = LLMClient(lambda: config, http_client=http)
        with pytest.raises(LLMError) as error:
            await client.chat_result(MESSAGES)
    assert error.value.status == 401 and len(calls) == 1


async def test_failover_rebudgets_for_smaller_provider(monkeypatch):
    monkeypatch.setattr("fllbot.llm.client.TRANSIENT_RETRY_DELAYS", ())
    captured = []

    def handler(req):
        captured.append((req.url.host, json.loads(req.content)))
        return httpx.Response(503) if req.url.host == "primary.test" else response()

    config = LLMConfig(
        api_key="k",
        max_tokens=200,
        default="primary",
        fallback_providers=["backup"],
        providers={
            "primary": {"base_url": "https://primary.test", "context_window": 20000},
            "backup": {"base_url": "https://backup.test", "context_window": 2000},
        },
    )
    messages = [
        MESSAGES[0],
        {"role": "user", "content": "old" * 2000},
        {"role": "assistant", "content": "old answer"},
        MESSAGES[1],
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        reply = await LLMClient(lambda: config, http_client=http).chat_result(messages)
    assert reply.provider == "backup"
    assert len(captured[0][1]["messages"]) == 4
    assert captured[1][1]["messages"] == MESSAGES


async def test_provider_concurrency_and_cancellation_release():
    entered, release = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def handler(req):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.set()
        try:
            await release.wait()
            return response()
        finally:
            active -= 1

    config = LLMConfig(api_key="k", context_window=8192, max_concurrency=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(lambda: config, http_client=http)
        first = asyncio.create_task(client.chat_result(MESSAGES))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(client.chat_result(MESSAGES))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await asyncio.wait_for(second, 1)).text == "ok"
    assert peak == 1 and client._active["default"] == 0


async def test_group_quota_serializes_and_survives_restart(tmp_path):
    llm = {"enabled": True, "api_key": "k", "context_window": 8192, "max_tokens": 20, "group_daily_token_limit": 1000}
    captured = []
    bot = await make_bot(tmp_path, builtin=False, llm=llm)
    try:
        bot.chat.client._http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: captured.append(req) or response(tokens=850))
        )
        results = await asyncio.gather(
            *[bot.chat.complete(MESSAGES, scene="group", scene_id=123) for _ in range(2)], return_exceptions=True
        )
        assert sum(isinstance(item, LLMError) for item in results) == 1
        assert len(captured) == 1
        await bot.chat.complete(MESSAGES, scene="group", scene_id=456)
        assert len(captured) == 2
    finally:
        await bot.stop()
    bot = await make_bot(tmp_path, builtin=False, llm=llm)
    try:
        with pytest.raises(LLMError, match="额度不足"):
            await bot.chat.complete(MESSAGES, scene="group", scene_id=123)
        yesterday = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() - 1
        await bot.storage.db.execute("UPDATE llm_usage SET created_at = ?", (yesterday,))
        await bot.storage.db.commit()
        bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response()))
        await bot.chat.complete(MESSAGES, scene="group", scene_id=123)
    finally:
        await bot.stop()


async def test_cost_quota_requires_prices_and_accounts_for_backup(tmp_path):
    bot = await make_bot(
        tmp_path,
        builtin=False,
        llm={
            "enabled": True,
            "api_key": "k",
            "context_window": 8192,
            "max_tokens": 20,
            "group_daily_cost_limit": 0.01,
            "default": "a",
            "fallback_providers": ["b"],
            "providers": {"a": {"input_price": 1, "output_price": 1}, "b": {}},
        },
    )
    try:
        with pytest.raises(LLMError, match="正数"):
            await bot.chat.complete(MESSAGES, scene="group", scene_id=123)
        bot.config_store.patch({"llm": {"providers": {"b": {"input_price": 10000, "output_price": 10000}}}})
        with pytest.raises(LLMError, match="费用额度不足"):
            await bot.chat.complete(MESSAGES, scene="group", scene_id=123)
    finally:
        await bot.stop()


async def test_truncated_tool_call_never_executes(tmp_path):
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "k", "context_window": 16000})
    try:
        await bot.plugins.get("agent").settings.update({"auto_group": True, "cooldown_seconds": 0})
        calls = [
            {"id": "c1", "type": "function", "function": {"name": "basic__check_online", "arguments": '{"broken":'}}
        ]
        bot.chat.client._http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: response("", calls=calls, finish="length"))
        )
        conn = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("check", at=10001)), conn)
        assert len(conn.texts()) == 1 and "未执行工具" in conn.texts()[0]
        assert not any(action != "send_group_msg" for action, _ in conn.calls)
        reply = await bot.chat.test("hello")
        assert reply.summary()["truncated"] is True and reply.tool_calls == []
    finally:
        await bot.stop()


def test_diagnostic_api_validates_functions_without_qq_actions(tmp_path):
    bot = build_bot(tmp_path, builtin=False, llm={"enabled": True, "api_key": "k", "context_window": 16000})
    captured = []

    def handler(req):
        body = json.loads(req.content)
        captured.append(body)
        if body.get("tools"):
            return response(
                "", calls=[{"id": "d1", "function": {"name": "diagnostic_echo", "arguments": '{"text":"FLLBOT_OK"}'}}]
            )
        return response("red" if isinstance(body["messages"][0]["content"], list) else "FLLBOT_OK")

    with TestClient(create_app(bot)) as api:
        bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert api.post("/api/llm/diagnose", json={}).status_code == 401
        assert api.post("/api/llm/diagnose", json={"provider": "missing"}, headers=AUTH).status_code == 400
        result = api.post("/api/llm/diagnose", json={}, headers=AUTH).json()
        assert result["ok"] is True
        assert [item["status"] for item in result["checks"]] == ["passed"] * 3
        assert len(captured) == 3
        assert api.get("/api/llm/usage?range=all", headers=AUTH).json()["requests"] == 3
        assert api.get("/api/chat/group/123", headers=AUTH).json()["messages"] == []


async def test_failover_after_tool_does_not_execute_tool_twice(tmp_path, monkeypatch):
    monkeypatch.setattr("fllbot.llm.client.TRANSIENT_RETRY_DELAYS", ())
    bot = await make_bot(
        tmp_path,
        llm={
            "enabled": True,
            "api_key": "k",
            "context_window": 16000,
            "default": "main",
            "fallback_providers": ["backup"],
            "providers": {
                "main": {"base_url": "https://primary.test"},
                "backup": {"base_url": "https://backup.test"},
            },
        },
    )
    captured, executed = [], []
    try:
        agent = bot.plugins.get("agent")
        await agent.settings.update(
            {"auto_group": True, "cooldown_seconds": 0, "allowed_tools": ["basic__check_online"]}
        )
        module = importlib.import_module("fllbot.builtin_plugins.agent.plugin")
        original = module._run_tool

        async def run_tool(*args):
            executed.append(args[1])
            return await original(*args)

        monkeypatch.setattr(module, "_run_tool", run_tool)

        def handler(req):
            body = json.loads(req.content)
            captured.append(body)
            if len(captured) == 1:
                return response(
                    "",
                    calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "basic__check_online",
                                "arguments": "{}",
                            },
                        }
                    ],
                )
            return httpx.Response(503) if req.url.host == "primary.test" else response("done")

        bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        conn = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("check", at=10001)), conn)
        assert executed == ["basic__check_online"]
        assert conn.texts() == ["done"]
        assert len(captured) == 3 and captured[1]["messages"] == captured[2]["messages"]
        assert captured[2]["messages"][-1]["role"] == "tool"
    finally:
        await bot.stop()


async def test_agent_executes_every_requested_tool_and_keeps_tools_available(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "k", "context_window": 16000})
    captured, executed = [], []
    try:
        await bot.plugins.get("agent").settings.update(
            {
                "auto_group": True,
                "cooldown_seconds": 0,
                "allowed_tools": ["basic__check_online"],
            }
        )
        module = importlib.import_module("fllbot.builtin_plugins.agent.plugin")
        original = module._run_tool

        async def run_tool(*args):
            executed.append(args[1])
            return await original(*args)

        monkeypatch.setattr(module, "_run_tool", run_tool)

        def handler(req):
            captured.append(json.loads(req.content))
            if len(captured) == 1:
                return response(
                    "",
                    calls=[
                        {
                            "id": str(i),
                            "type": "function",
                            "function": {
                                "name": "basic__check_online",
                                "arguments": "{}",
                            },
                        }
                        for i in range(5)
                    ],
                )
            return response("检查完成")

        bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        conn = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("check", at=10001)), conn)
        assert executed == ["basic__check_online"] * 5
        assert len(captured) == 2 and captured[-1]["tools"]
        results = [item for item in captured[-1]["messages"] if item["role"] == "tool"]
        assert len(results) == 5
        assert conn.texts() == ["检查完成"]
    finally:
        await bot.stop()


async def test_stream_without_streaming_does_not_deadlock():
    config = LLMConfig(api_key="k", context_window=8192, providers={"default": {"no_stream": True}}, max_concurrency=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response("hello"))) as http:
        client = LLMClient(lambda: config, http_client=http)

        async def collect():
            return [piece async for piece in client.chat_stream(MESSAGES)]

        assert await asyncio.wait_for(collect(), 1) == ["hello"]


async def test_plain_truncated_reply_preserves_text_and_reports_reason(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, llm={"enabled": True, "api_key": "k", "context_window": 8192})
    try:
        bot.chat.client._http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: response("partial", finish="length"))
        )
        reply = await bot.chat.test("write")
        assert reply.text.startswith("partial") and "继续" in reply.text
        assert reply.summary()["finish_reason"] == "length" and reply.summary()["truncated"] is True
    finally:
        await bot.stop()


def test_per_model_capabilities_override_global_defaults():
    config = LLMConfig(
        context_window=16000,
        supports_images=False,
        providers={
            "small": {"context_window": 2000, "supports_images": True, "supports_tools": False},
            "inherited": {},
        },
    )
    assert config.resolve("small")[1].context_window == 2000
    assert config.resolve("small")[1].supports_images is True
    assert config.resolve("small")[1].supports_tools is False
    assert config.resolve("inherited")[1].context_window == 16000
    assert config.resolve("inherited")[1].supports_images is False


def test_model_controls_validate_persist_and_diagnostics_skip(tmp_path):
    llm = {"enabled": True, "api_key": "private-test-key", "context_window": 16000}
    with TestClient(create_app(build_bot(tmp_path, builtin=False, llm=llm))) as api:
        result = api.patch(
            "/api/config",
            headers=AUTH,
            json={
                "llm": {
                    "supports_tools": False,
                    "supports_images": False,
                    "supports_temperature": False,
                    "token_parameter": "max_completion_tokens",
                    "max_concurrency": 1,
                    "input_price": 1,
                    "output_price": 2,
                    "group_daily_token_limit": 10000,
                }
            },
        )
        assert result.status_code == 200
        assert (
            api.patch("/api/config", headers=AUTH, json={"llm": {"fallback_providers": ["missing"]}}).status_code == 400
        )
        assert api.patch("/api/config", headers=AUTH, json={"llm": {"max_concurrency": 0}}).status_code == 400
    bot = build_bot(tmp_path, builtin=False, llm=llm)
    with TestClient(create_app(bot)) as api:
        assert bot.config.llm.group_daily_token_limit == 10000
        status = api.get("/api/llm/providers", headers=AUTH).json()[0]
        assert status["supports_tools"] is False and status["input_price"] == 1
        assert status["max_concurrency"] == 1 and status["token_parameter"] == "max_completion_tokens"
        bot.chat.client._http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(400, json={"error": {"message": "private-test-key: invalid"}})
            )
        )
        result = api.post("/api/llm/diagnose", headers=AUTH, json={})
        assert "private-test-key" not in result.text
        assert [check["status"] for check in result.json()["checks"]] == ["failed", "skipped", "skipped"]

@pytest.mark.parametrize("recover", [True, False])
async def test_empty_answer_retries_once_and_reports_finish_reason(recover):
    calls = []

    def handler(req):
        calls.append(req)
        return response("ok") if recover and len(calls) == 2 else response(None, finish="length")

    config = LLMConfig(api_key="k", context_window=8192)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(lambda: config, http_client=http)
        if recover:
            assert (await client.chat_result(MESSAGES)).text == "ok"
        else:
            with pytest.raises(LLMError, match="finish_reason=length"):
                await client.chat_result(MESSAGES)
    assert len(calls) == 2


async def test_empty_answer_can_fail_over():
    calls = []

    def handler(req):
        calls.append(req.url.host)
        return response(None) if req.url.host == "primary.test" else response("backup")

    config = LLMConfig(
        api_key="k", context_window=8192, default="primary", fallback_providers=["backup"],
        providers={"primary": {"base_url": "https://primary.test"},
                   "backup": {"base_url": "https://backup.test"}},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await LLMClient(lambda: config, http_client=http).chat_result(MESSAGES)
    assert result.text == "backup"
    assert calls == ["primary.test", "primary.test", "backup.test"]


async def test_content_filter_is_not_retried():
    calls = []
    config = LLMConfig(api_key="k", context_window=8192)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: calls.append(req) or response(None, finish="content_filter"))
    ) as http:
        with pytest.raises(LLMError, match="内容过滤"):
            await LLMClient(lambda: config, http_client=http).chat_result(MESSAGES)
    assert len(calls) == 1
