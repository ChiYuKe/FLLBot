from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, build_bot, group_message, make_bot

AUTH = {"Authorization": "Bearer admin-token"}


def test_chat_options_require_auth_and_survive_restart(tmp_path: Path):
    with TestClient(create_app(build_bot(tmp_path))) as client:
        assert client.get('/api/console/chat').status_code == 401
        options = client.get('/api/console/chat', headers=AUTH).json()
        options.update(send_nickname=False, send_time=True, allow_images=True, read_forward=True, wake_word='bot', wake_only_mode='raw', wake_only_replies=['我在', '怎么啦'])
        assert client.put('/api/console/chat', headers=AUTH, json=options).status_code == 200
    with TestClient(create_app(build_bot(tmp_path))) as client:
        assert client.get('/api/console/chat', headers=AUTH).json() == options


async def test_disabled_metadata_is_removed_from_current_and_history(tmp_path: Path):
    bot = await make_bot(tmp_path, builtin=False, llm={'context_mode': 'custom'}, overrides={'chat': {
        'send_nickname': False, 'send_user_id': False, 'send_role': False, 'send_time': False,
    }})
    try:
        await bot.storage.add_chat_message('group', 20002, 'user', '历史正文', user_id=40004, nickname='历史昵称', sender_role='admin')
        ctx = Context(bot=bot, connection=FakeConnection(), event=parse_event(group_message('当前正文', nickname='当前昵称')))
        messages = await bot.chat.build_messages('group', 20002, '当前正文', ctx=ctx)
        assert [m['content'] for m in messages[1:]] == ['历史正文', '当前正文']
        assert all(value == '' for key, value in bot.chat._variables('group', 20002, ctx).items() if key in {'user_id', 'user_name', 'nickname', 'time', 'date', 'weekday'})
        await bot.update_config({'chat': {'send_nickname': True, 'send_time': True}})
        messages = await bot.chat.build_messages('group', 20002, '当前正文', ctx=ctx)
        assert '历史昵称：历史正文' in messages[1]['content']
        assert '当前昵称：当前正文' in messages[-1]['content']
        assert messages[1]['content'].startswith('[')
        assert '40004' not in messages[1]['content']
    finally:
        await bot.stop()


@pytest.mark.parametrize('allowed', [False, True])
async def test_images_are_attached_only_with_permission(tmp_path: Path, allowed):
    bot = await make_bot(tmp_path, builtin=False, llm={'context_mode': 'custom'}, overrides={'chat': {'allow_images': allowed}})
    try:
        payload = group_message('看图')
        payload['message'].append({'type': 'image', 'data': {'url': 'https://example.test/image.png'}})
        ctx = Context(bot=bot, connection=FakeConnection(), event=parse_event(payload))
        messages = await bot.chat.build_messages('group', 20002, '看图', ctx=ctx)
        content = messages[-1]['content']
        if allowed:
            assert content[1] == {'type': 'image_url', 'image_url': {'url': 'https://example.test/image.png'}}
        else:
            assert isinstance(content, str)
            assert 'image.png' not in content
    finally:
        await bot.stop()


@pytest.mark.parametrize('mode,expected', [('prompt', '自定义的唤醒提示'), ('raw', 'bot'), ('ignore', None)])
async def test_wake_only_behavior_uses_chat_options(tmp_path: Path, monkeypatch, mode, expected):
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake-test-key', 'context_mode': 'custom'}, overrides={'chat': {
        'send_nickname': False, 'send_user_id': False, 'send_role': False,
        'wake_word': 'bot', 'wake_only_mode': mode, 'wake_only_prompt': '自定义的唤醒提示',
    }})
    try:
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {'auto_group': True, 'trigger_modes': ['prefix'], 'cooldown_seconds': 0}})
        complete = AsyncMock(return_value=SimpleNamespace(text='我在', tool_calls=[]))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message('bot')), connection)
        if expected is None:
            complete.assert_not_awaited()
            assert connection.texts() == []
        else:
            assert complete.call_args.args[0][-1]['content'] == expected
            rows = await bot.storage.recent_chat('group', 20002)
            assert rows[0].content == 'bot'
    finally:
        await bot.stop()


async def test_wake_only_fixed_replies_without_model(tmp_path: Path, monkeypatch):
    # llm 未启用，固定回复仍应直接发出，证明不依赖模型。
    bot = await make_bot(tmp_path, overrides={'chat': {
        'send_nickname': False, 'send_user_id': False, 'send_role': False,
        'wake_word': 'bot', 'wake_only_mode': 'fixed', 'wake_only_replies': ['我在', '怎么啦'],
    }})
    try:
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {'auto_group': True, 'trigger_modes': ['prefix'], 'cooldown_seconds': 0}})
        complete = AsyncMock(return_value=SimpleNamespace(text='不该调用模型', tool_calls=[]))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message('bot')), connection)
        complete.assert_not_awaited()
        assert connection.texts() in (['我在'], ['怎么啦'])
        rows = await bot.storage.recent_chat('group', 20002)
        assert [row.role for row in rows] == ['user', 'assistant']
        assert rows[0].content == 'bot'
        assert rows[1].content == connection.texts()[0]
        usage = await bot.storage.llm_usage_summary(0)
        assert usage['requests'] == 0
    finally:
        await bot.stop()


@pytest.mark.parametrize('replies', [[], ['   ']])
async def test_wake_only_fixed_without_texts_stays_silent(tmp_path: Path, monkeypatch, replies):
    bot = await make_bot(tmp_path, overrides={'chat': {
        'wake_word': 'bot', 'wake_only_mode': 'fixed', 'wake_only_replies': replies,
    }})
    try:
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {'auto_group': True, 'trigger_modes': ['prefix'], 'cooldown_seconds': 0}})
        complete = AsyncMock(return_value=SimpleNamespace(text='不该调用模型', tool_calls=[]))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message('bot')), connection)
        complete.assert_not_awaited()
        assert connection.texts() == []
        assert await bot.storage.recent_chat('group', 20002) == []
    finally:
        await bot.stop()
