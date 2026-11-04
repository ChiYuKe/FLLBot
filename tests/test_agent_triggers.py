from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot


async def test_agent_history_start_is_scoped_to_account_and_group(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,llm={'enabled':True,'api_key':'fake','context_window':8192})
    try:
        await bot.permissions.save_group(20002,enabled=True,config={'policy':{
            'auto_group':True,'trigger_modes':['all'],'cooldown_seconds':0}})
        complete = AsyncMock(return_value=SimpleNamespace(text='answer',tool_calls=[]))
        monkeypatch.setattr(bot.chat,'complete',complete)
        for account in (11,22):
            payload = group_message('hello')
            payload['self_id'] = account
            await bot.dispatcher.dispatch(parse_event(payload),FakeConnection(self_id=account))
        assert complete.await_count == 2
        assert await bot.storage.kv_get('agent-consent-start','11:20002') is not None
        assert await bot.storage.kv_get('agent-consent-start','22:20002') is not None
        assert await bot.storage.kv_get('agent-consent-start','20002') is None
    finally:
        await bot.stop()


@pytest.mark.parametrize('variant', ['tool', 'marker', 'no_tools', 'normal_reply'])
async def test_non_participation_is_internal_and_never_announced(tmp_path, monkeypatch, variant):
    bot = await make_bot(tmp_path, llm={
        'enabled': True, 'api_key': 'fake-test-key', 'context_window': 8192,
        'supports_tools': variant != 'no_tools',
    })
    try:
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {
            'auto_group': True, 'trigger_modes': ['all'], 'cooldown_seconds': 0,
            'allowed_tools': ['basic__check_online'],
        }})
        if variant == 'no_tools':
            await bot.permissions.save_group(20002, enabled=True, config={'policy': {
                'auto_group': True, 'trigger_modes': ['all'], 'cooldown_seconds': 0, 'allowed_tools': [],
            }})
        external_tool = bot.plugins.get('basic').ai_tools[0]
        external_call = AsyncMock(return_value={'ok': True})
        monkeypatch.setattr(external_tool, 'callback', external_call)
        text = '这是群主在问其他人，没有点到我，所以我不接话。'
        calls = []
        if variant == 'tool':
            calls = [
                {'id': 'external', 'type': 'function', 'function': {'name': 'basic__check_online', 'arguments': '{}'}},
                {'id': 'quiet', 'type': 'function', 'function': {'name': 'agent__stay_silent', 'arguments': '{}'}},
            ]
        elif variant in ('marker', 'no_tools'):
            text = '<NO_REPLY>'
        else:
            text = '刚才以为你在问别人，原来是在叫我呀。'
        complete = AsyncMock(return_value=SimpleNamespace(text=text, tool_calls=calls))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        content = '你为什么不接话？' if variant == 'normal_reply' else '真的？'
        await bot.dispatcher.dispatch(parse_event(group_message(content)), connection)
        complete.assert_awaited_once()
        assert connection.texts() == ([text] if variant == 'normal_reply' else [])
        external_call.assert_not_awaited()
        if variant != 'normal_reply':
            assert not await bot.storage.recent_chat('group', 20002)
            raw = await bot.chat.memory.store.recent(10001, 20002, 9999)
            assert [r['content'] for r in raw] == ['真的？']
        if variant == 'no_tools':
            assert complete.call_args.kwargs['tools'] is None
        assert '不要宣布' in complete.call_args.args[0][0]['content']
    finally:
        await bot.stop()


@pytest.mark.parametrize(("modes", "text", "mention", "expected"), [
    (["at", "prefix"], "路过闲聊", None, False),
    (["at", "prefix"], "你好", 10001, True),
    (["at", "prefix"], "木鱼，你好", None, True),
    (["at", "prefix"], "木鱼，你好", 10001, True),
    (["at", "prefix"], "木鱼，你好", 99999, False),
    (["at", "prefix"], "木鱼", None, True),
    (["all", "at"], "你好", None, True),
    (["all", "prefix"], "普通闲聊", None, True),
    (["all", "prefix"], "木鱼，你好", None, True),
    (["all", "prefix"], "木鱼，你好", 99999, False),
    (["prefix"], "木鱼，你好", 99999, False),
    (["prefix"], "木鱼，你好", "all", True),
    (["at"], "木鱼，你好", None, False),
    (["prefix"], "你好", 10001, False),
])
async def test_combined_triggers_match_any_condition_only_once(tmp_path, monkeypatch, modes, text, mention, expected):
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake-test-key", "context_window": 8192})
    try:
        await bot.permissions.save_group(20002, enabled=True, config={"policy": {
            "auto_group": True, "trigger_modes": modes, "cooldown_seconds": 0,
        }})
        complete = AsyncMock(return_value=SimpleNamespace(text="收到", tool_calls=[]))
        monkeypatch.setattr(bot.chat, "complete", complete)
        connection = FakeConnection()
        connection.nickname = "木鱼"
        await bot.dispatcher.dispatch(parse_event(group_message(text, at=mention)), connection)
        assert complete.await_count == int(expected)
        assert len(connection.texts()) == int(expected)
        if expected and "prefix" in modes and text.startswith("木鱼") and text != "木鱼":
            assert complete.call_args.args[0][-1]["content"].endswith("你好")
            assert "木鱼，你好" not in complete.call_args.args[0][-1]["content"]
    finally:
        await bot.stop()


async def test_mention_after_wake_word_still_replies(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake-test-key", "context_window": 8192})
    try:
        await bot.permissions.save_group(20002, enabled=True, config={"policy": {
            "auto_group": True, "trigger_modes": ["prefix"], "cooldown_seconds": 0,
        }})
        complete = AsyncMock(return_value=SimpleNamespace(text="收到", tool_calls=[]))
        monkeypatch.setattr(bot.chat, "complete", complete)
        connection = FakeConnection()
        connection.nickname = "木鱼"
        message = [
            {"type": "text", "data": {"text": "木鱼 "}},
            {"type": "at", "data": {"qq": "99999"}},
            {"type": "text", "data": {"text": " 你好"}},
        ]
        await bot.dispatcher.dispatch(parse_event(group_message("", message=message)), connection)
        assert complete.await_count == 1
        assert complete.call_args.args[0][-1]["content"].endswith("你好")
    finally:
        await bot.stop()


@pytest.mark.parametrize(("mode", "text", "mention", "expected", "expected_content"), [
    ("at", "你好", None, False, ""),
    ("at", "你好", 10001, True, "测试用户(30003，成员)：你好"),
    ("prefix", "你好", None, False, ""),
    ("prefix", "木鱼 你好", None, True, "测试用户(30003，成员)：你好"),
    ("prefix", "木鱼", None, True, "测试用户(30003，成员)：（用户只叫了你的名字"),
    ("all", "你好", None, True, "测试用户(30003，成员)：你好"),
])
async def test_agent_trigger_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    text: str,
    mention: int | None,
    expected: bool,
    expected_content: str,
) -> None:
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake-test-key", "context_window": 8192})
    try:
        agent = bot.plugins.get("agent")
        assert agent is not None
        await agent.settings.update({"auto_group": True, "trigger_mode": mode, "cooldown_seconds": 0})
        complete = AsyncMock(return_value=SimpleNamespace(text="收到", tool_calls=[]))
        monkeypatch.setattr(bot.chat, "complete", complete)
        connection = FakeConnection()
        connection.nickname = "木鱼"
        await bot.dispatcher.dispatch(parse_event(group_message(text, at=mention)), connection)
        assert complete.await_count == int(expected)
        assert bool(connection.texts()) is expected
        if expected:
            assert complete.call_args.args[0][-1]["content"].startswith(expected_content)
        if mode == "prefix" and text == "木鱼":
            # 内部提示只发给模型，历史里仍记录用户原话。
            rows = await bot.storage.recent_chat("group", 20002, self_id=10001)
            assert [row.content for row in rows if row.role == "user"] == ["木鱼"]
    finally:
        await bot.stop()
