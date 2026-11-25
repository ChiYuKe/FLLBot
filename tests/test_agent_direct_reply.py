"""直出快路径：silent+reply 整批提前收尾、混合批次继续模型、失败与语音语义。"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.llm.agent_runtime import AgentRuntime
from fllbot.onebot.connection import ActionError
from helpers import FakeConnection, group_message, make_bot

GROUP = 20002
SELF_ID = 10001
LLM = {"enabled": True, "api_key": "fake", "context_window": 16000}

_DIRECT_PLUGIN = """
from fllbot.plugin import Plugin

plugin = Plugin('directdemo', enabled_by_default=True)
calls = []


@plugin.on_ai_tool(
    'deliver', description='把整理好的资料交付给用户',
    parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
    effect='side_effecting',
    capabilities=('qq.send',),
)
async def deliver(ctx):
    calls.append('deliver')
    return {'sent': True, 'silent': True, 'reply': '资料已经发给你了。'}


@plugin.on_ai_tool(
    'notify', description='再补一条交付通知',
    parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
    effect='side_effecting',
    capabilities=('qq.send',),
)
async def notify(ctx):
    calls.append('notify')
    return {'sent': True, 'silent': True, 'reply': '通知也发好了。'}


@plugin.on_ai_tool(
    'lookup', description='读取统计数据',
    parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
    effect='read_only',
)
async def lookup(ctx):
    calls.append('lookup')
    return {'items': [1, 2, 3]}


@plugin.on_ai_tool(
    'unsent', description='发送未成功的交付',
    parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
    effect='side_effecting',
)
async def unsent(ctx):
    calls.append('unsent')
    return {'sent': False, 'silent': True, 'reply': '这次没有发出去。'}


@plugin.on_ai_tool(
    'speak', description='用语音表达结果',
    parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
    effect='side_effecting',
)
async def speak(ctx):
    calls.append('speak')
    return {'silent': True, 'spoken': '语音内容', 'completed': True}
"""


def _write_plugins(tmp_path: Path, sources: dict[str, str]) -> Path:
    directory = tmp_path / "plugins"
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        (directory / f"{name}.py").write_text(source, encoding="utf-8")
    return directory


async def _configure(bot, *, tools: list[str]) -> None:
    await bot.permissions.save_group(GROUP, enabled=True, config={"policy": {
        "auto_group": True,
        "trigger_modes": ["at"],
        "cooldown_seconds": 0,
        "allowed_tools": list(tools),
    }})


async def _settle(bot, timeout: float = 3.0) -> None:
    await asyncio.wait_for(bot.dispatcher._queue.join(), timeout)
    pending = [task for task in bot.agent_tasks.acknowledgements if not task.done()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), timeout)


async def _deliver(bot, connection, payload: dict) -> None:
    await bot.on_raw_event(connection, payload)
    await _settle(bot)


def _tool_call(call_id: str, name: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


def _tool_results(messages: list[dict]) -> dict[str, dict]:
    return {message["tool_call_id"]: json.loads(message["content"])
            for message in messages if message.get("role") == "tool"}


async def _chat(bot) -> list[tuple[str, str]]:
    rows = await bot.storage.recent_chat("group", GROUP, self_id=SELF_ID)
    return [(row.role, row.content) for row in rows]


async def _turn(bot):
    turns = await bot.storage.list_turns()
    assert len(turns["items"]) == 1, turns["items"]
    return await bot.storage.turn_detail(turns["items"][0]["turn_id"])


# --------------------------------------------------------------------------- #
# Runtime 单元：合并多个 silent reply
# --------------------------------------------------------------------------- #


async def test_runtime_merges_deduplicates_and_short_circuits_silent_batch():
    calls = [_tool_call("a", "deliver"), _tool_call("b", "notify"), _tool_call("c", "deliver")]
    requests = []

    async def model(messages, **options):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=calls)
        return SimpleNamespace(text="不应继续", tool_calls=[])

    outputs = iter([
        ('{"sent": true, "silent": true, "reply": "甲"}', True),
        ('{"sent": true, "silent": true, "reply": "乙"}', True),
        ('{"sent": true, "silent": true, "reply": "甲"}', True),
    ])

    async def execute(ctx, name, arguments, registry):
        return next(outputs)

    result = await AgentRuntime(model).run(
        [], scene="group", scene_id=123,
        ctx=SimpleNamespace(data_current=True, self_id=11, tool_call_id=""),
        registry={name: None for name in ("deliver", "notify")},
        execute=execute, agent=True,
    )
    assert result.reason == "tool_completed"
    assert result.reply == "甲\n乙"
    assert result.text == ""
    assert len(requests) == 1


# --------------------------------------------------------------------------- #
# 端到端：直出、混合批次、失败、语音
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("error", "expected"), [
    (None, "submitted"),
    (ActionError("send_friend_request", retcode=1404), "unsupported"),
    (ActionError("send_friend_request", message="调用超时"), "tool_uncertain"),
])
async def test_group_friend_request_targets_only_current_qq_sender(tmp_path, monkeypatch, error, expected):
    bot = await make_bot(tmp_path, llm=LLM)
    connection = FakeConnection()
    connection.send_friend_request = AsyncMock(side_effect=error)
    name = "chat_dispatch__request_friendship"
    malicious = _tool_call("other-target", name)
    malicious["function"]["arguments"] = '{"user_id":99999}'
    results = {}

    async def complete(messages, **options):
        if not (outputs := _tool_results(messages)):
            assert name in {tool["function"]["name"] for tool in options["tools"]}
            return SimpleNamespace(text="", tool_calls=[
                malicious, _tool_call("current-sender", name), _tool_call("duplicate", name),
            ])
        results.update(outputs)
        return SimpleNamespace(text=outputs["current-sender"]["message"], tool_calls=[])

    try:
        await _configure(bot, tools=[name])
        monkeypatch.setattr(bot.chat, "complete", complete)
        event = group_message("加我好友吧", at=SELF_ID, message_id=7)
        await _deliver(bot, connection, event)
        connection.send_friend_request.assert_awaited_once_with(
            event["user_id"], message="你好，我是群里收到你请求的机器人。"
        )
        assert results["other-target"]["error"] == "invalid_arguments"
        result = results["current-sender"]
        assert result.get("status", result.get("error")) == expected
        assert results["duplicate"] == result
        assert connection.texts() == [result["message"]]
    finally:
        await bot.stop()


async def test_silent_reply_tool_requests_model_once_and_sends_reply(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"directdemo": _DIRECT_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    requests: list[list[dict]] = []

    async def complete(messages, **options):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=[_tool_call("deliver-1", "directdemo__deliver")])
        return SimpleNamespace(text="不应再请求模型", tool_calls=[])

    try:
        await _configure(bot, tools=["directdemo__deliver"])
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("把资料整理好发我", at=SELF_ID, message_id=7))

        assert len(requests) == 1, "整批 silent 时不应再请求模型"
        assert connection.texts() == ["资料已经发给你了。"]
        segments = connection.sent[0][2]
        assert any(segment.type == "reply" and str(segment.get("id")) == "7" for segment in segments), \
            "直出 reply 必须走既有发送回调，保留引用"
        assert await _chat(bot) == [
            ("user", "把资料整理好发我"), ("assistant", "资料已经发给你了。")]
        detail = await _turn(bot)
        assert detail["status"] == "delivered"
        assert [item["tool"] for item in detail["tools"]] == ["directdemo__deliver"]
    finally:
        await bot.stop()


async def test_copyable_text_request_recovers_when_model_calls_image_tool(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM, overrides={"chat": {
        "reply_mode": "image", "reply_interval_ms": 0,
    }})
    connection = FakeConnection()
    answer = "正向：clean lineart, soft shadows\n负向：extra fingers"
    call = _tool_call("format-image", "text__render_image")
    call["function"]["arguments"] = json.dumps({"text": answer})
    requests = []

    async def complete(messages, **options):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=[call])
        return SimpleNamespace(text=answer, tool_calls=[])

    try:
        await _configure(bot, tools=["text__render_image"])
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("不要转图片，直接发可复制文本", at=SELF_ID))

        assert len(requests) == 2
        assert any("本轮要求可直接复制的文本" in message["content"] for message in requests[0]
                   if message["role"] == "system")
        assert "可直接复制" in _tool_results(requests[1])["format-image"]["error"]
        assert connection.texts() == [answer]
        assert all(segment.type in {"reply", "at", "text"} for _, _, segments in connection.sent
                   for segment in segments)
    finally:
        await bot.stop()


@pytest.mark.parametrize("request_text, marker, expect_forward", [
    ("给我一份可以直接复制的", "<REPLY_FORWARD>", True),
    ("不要转图片，直接发可复制文本", "<REPLY_FORWARD>", False),
    ("介绍一下这个项目", "<REPLY_TEXT>", False),
])
async def test_model_selects_qq_reply_format(tmp_path, monkeypatch, request_text, marker, expect_forward):
    bot = await make_bot(tmp_path, llm=LLM, overrides={"chat": {
        "reply_mode": "auto_image", "forward_threshold": 200, "reply_interval_ms": 0,
    }})
    connection = FakeConnection()
    connection.send_group_forward_msg = AsyncMock(return_value={"message_id": 1234})
    answer = "第一段。\n\n    第二段要保留缩进。" * 16
    complete = AsyncMock(return_value=SimpleNamespace(text=f"{marker}\n{answer}", tool_calls=[]))

    try:
        await _configure(bot, tools=[])
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message(request_text, at=SELF_ID))

        complete.assert_awaited_once()
        rules = complete.call_args.args[0][0]["content"]
        assert ("<REPLY_FORWARD>" in rules) == (not request_text.startswith("不要转图片"))
        if expect_forward:
            connection.send_group_forward_msg.assert_awaited_once()
            nodes = connection.send_group_forward_msg.await_args.args[1]
            assert "".join(segment["data"]["text"] for node in nodes for segment in node["data"]["content"]
                           if segment["type"] == "text" and segment["data"]["text"] != " ") == answer
            assert not connection.sent
        else:
            connection.send_group_forward_msg.assert_not_awaited()
            assert connection.texts() == [answer]
    finally:
        await bot.stop()


@pytest.mark.parametrize("opening", ["<REPLY_TEXT>\n", ""])
async def test_long_copyable_prompt_uses_one_chat_record_and_strips_closing_marker(tmp_path, monkeypatch, opening):
    bot = await make_bot(tmp_path, llm=LLM, overrides={"chat": {
        "reply_mode": "auto_image", "reply_chunk_size": 180, "reply_interval_ms": 0,
    }})
    connection = FakeConnection()
    connection.send_group_forward_msg = AsyncMock(return_value={"message_id": 1234})
    positive = "soft lighting, white background, clean lineart, " * 8
    answer = f"正向提示词\n```\n{positive}\n```\n\n负向提示词\n```\nblurry, watermark\n```"
    complete = AsyncMock(return_value=SimpleNamespace(
        text=f"{opening}{answer}\n</REPLY_TEXT>", tool_calls=[]
    ))
    try:
        await _configure(bot, tools=[])
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("帮我分析图片，给我可复制的提示词", at=SELF_ID))

        assert "180 字符" in complete.call_args.args[0][0]["content"]
        connection.send_group_forward_msg.assert_awaited_once()
        assert not connection.sent
        nodes = connection.send_group_forward_msg.await_args.args[1]
        assert len(nodes) >= 3
        assert "".join(node["data"]["content"][-1]["data"]["text"] for node in nodes) == answer
        assert (await _chat(bot))[-1] == ("assistant", answer)
    finally:
        await bot.stop()


async def test_chat_dispatch_can_be_disabled_per_group(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM, overrides={"chat": {
        "reply_mode": "auto_image", "reply_interval_ms": 0,
    }})
    connection = FakeConnection()
    connection.send_group_forward_msg = AsyncMock(return_value={"message_id": 1234})
    complete = AsyncMock(return_value=SimpleNamespace(
        text="<REPLY_FORWARD>\n这条是普通群消息。", tool_calls=[]
    ))
    try:
        entry = bot.plugins.entry_for("chat_dispatch")
        assert entry is not None and entry.builtin and entry.plugin.enabled_by_default
        await _configure(bot, tools=[])
        await bot.permissions.set_plugin_enabled("chat_dispatch", False, GROUP)
        assert bot.permissions.plugin_enabled("chat_dispatch", GROUP + 1)
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("介绍一下项目", at=SELF_ID))

        assert "<REPLY_FORWARD>" not in complete.call_args.args[0][0]["content"]
        connection.send_group_forward_msg.assert_not_awaited()
        assert connection.texts() == ["这条是普通群消息。"]
    finally:
        await bot.stop()


async def test_chat_dispatch_covers_all_shipped_tool_plugins(tmp_path):
    root = Path(__file__).resolve().parents[1]
    plugin_dir = tmp_path / "plugins"
    external = sorted((root / "plugins").glob("*/plugin.toml"))
    builtin = sorted((root / "fllbot" / "builtin_plugins").glob("*/plugin.toml"))
    for manifest in external:
        shutil.copytree(manifest.parent, plugin_dir / manifest.parent.name, ignore=shutil.ignore_patterns("__pycache__"))
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    try:
        assert set(bot.plugins.names()) == {manifest.parent.name for manifest in [*builtin, *external]}
        assert not bot.plugins.failed
        routing = bot.plugins.get("chat_dispatch").chat_dispatch
        snapshot = bot.tools.snapshot()
        for owner in snapshot.plugins:
            visible = {entry.name for entry in snapshot.by_plugin(owner)}
            hints = routing.tool_instructions(visible)
            assert "\n- " in hints, owner
            assert hints.count("\n- ") == 1, owner
        assert "web__weather_card" not in routing.tool_instructions({"web__get_weather"})
        assert "github__render_card" not in routing.tool_instructions({"github__read_repository"})
        assert "github__render_card" in routing.tool_instructions({"github__render_card"})
    finally:
        await bot.stop()


async def test_chat_dispatch_reuses_delivery_with_new_call_id(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"directdemo": _DIRECT_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    complete = AsyncMock(return_value=SimpleNamespace(text="", tool_calls=[
        _tool_call("delivery-1", "directdemo__deliver"),
        _tool_call("delivery-2", "directdemo__deliver"),
    ]))
    try:
        await _configure(bot, tools=["directdemo__deliver"])
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("请把资料发到群里", at=SELF_ID))

        complete.assert_awaited_once()
        assert bot.plugins.get("directdemo").module.calls == ["deliver"]
        assert connection.texts() == ["资料已经发给你了。"]
        assert len((await _turn(bot))["tools"]) == 1
    finally:
        await bot.stop()


async def test_chat_dispatch_does_not_replay_uncertain_delivery(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"directdemo": _DIRECT_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    requests = []

    async def uncertain(ctx):
        raise TimeoutError("connection timed out after submission")

    async def complete(messages, **options):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=[
                _tool_call("first", "directdemo__notify"),
                _tool_call("retry", "directdemo__notify"),
            ])
        return SimpleNamespace(text="发送结果暂时无法确认。", tool_calls=[])

    try:
        await _configure(bot, tools=["directdemo__notify"])
        spec = next(spec for spec in bot.plugins.get("directdemo").ai_tools if spec.name == "notify")
        spec.callback = AsyncMock(side_effect=uncertain)
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("发个通知", at=SELF_ID))

        spec.callback.assert_awaited_once()
        results = _tool_results(requests[1])
        assert {result["error"] for result in results.values()} == {"tool_uncertain"}
        assert connection.texts() == ["发送结果暂时无法确认。"]
    finally:
        await bot.stop()


async def test_mixed_silent_and_data_batch_continues_without_losing_results(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"directdemo": _DIRECT_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    requests: list[list[dict]] = []

    async def complete(messages, **options):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=[
                _tool_call("deliver-1", "directdemo__deliver"),
                _tool_call("lookup-1", "directdemo__lookup"),
            ])
        return SimpleNamespace(text="资料已发，一共 3 条数据。", tool_calls=[])

    try:
        await _configure(bot, tools=["directdemo__deliver", "directdemo__lookup"])
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("发资料并告诉我条数", at=SELF_ID, message_id=1))

        assert len(requests) == 2, "混合 silent + 数据批次必须继续下一轮模型"
        results = _tool_results(requests[1])
        assert results["deliver-1"]["reply"] == "资料已经发给你了。"
        assert results["lookup-1"] == {"items": [1, 2, 3]}, "数据结果不能丢"
        assert connection.texts() == ["资料已发，一共 3 条数据。"]
        assert await _chat(bot) == [("user", "发资料并告诉我条数"), ("assistant", "资料已发，一共 3 条数据。")]
    finally:
        await bot.stop()


async def test_failed_silent_batch_is_not_short_circuited(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"directdemo": _DIRECT_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    requests: list[list[dict]] = []

    async def complete(messages, **options):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=[_tool_call("unsent-1", "directdemo__unsent")])
        return SimpleNamespace(text="这次没有发出去，请稍后再试。", tool_calls=[])

    try:
        await _configure(bot, tools=["directdemo__unsent"])
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("发一下", at=SELF_ID, message_id=1))

        assert len(requests) == 2, "失败批次不能提前收尾"
        assert connection.texts() == ["这次没有发出去，请稍后再试。"]
        reminder_kinds = [item.get("_fllbot_source", {}).get("kind")
                          for item in requests[1] if item.get("_fllbot_source")]
        assert "tool_failure" in reminder_kinds
    finally:
        await bot.stop()


async def test_voice_spoken_only_does_not_send_text(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"directdemo": _DIRECT_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    requests: list[list[dict]] = []

    async def complete(messages, **options):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=[_tool_call("speak-1", "directdemo__speak")])
        return SimpleNamespace(text="不应再请求模型", tool_calls=[])

    try:
        await _configure(bot, tools=["directdemo__speak"])
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("用语音回答我", at=SELF_ID, message_id=1))

        assert len(requests) == 1
        assert connection.sent == [], "只有 spoken 的工具不能退化成文字发送"
        assert await _chat(bot) == [("user", "用语音回答我")]
        detail = await _turn(bot)
        assert (detail["status"], detail["reason"]) == ("silent", "tool_completed")
    finally:
        await bot.stop()


async def test_multiple_silent_tool_replies_are_merged_into_one_text(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"directdemo": _DIRECT_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    requests: list[list[dict]] = []

    async def complete(messages, **options):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=[
                _tool_call("deliver-1", "directdemo__deliver"),
                _tool_call("notify-1", "directdemo__notify"),
            ])
        return SimpleNamespace(text="不应再请求模型", tool_calls=[])

    try:
        await _configure(bot, tools=["directdemo__deliver", "directdemo__notify"])
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("都发一下", at=SELF_ID, message_id=1))

        assert len(requests) == 1
        assert connection.texts() == ["资料已经发给你了。\n通知也发好了。"]
        assert "不应再请求模型" not in connection.texts()
    finally:
        await bot.stop()
