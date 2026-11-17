"""私聊智能体：场景隔离、开关、工具授权、沙盒与任务控制。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot, private_message

LLM = {"enabled": True, "api_key": "fake", "context_window": 16000}


def reply(text='', calls=None):
    return SimpleNamespace(text=text, tool_calls=calls or [])


def tool_call(name, arguments='{}', call_id='call-1'):
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


async def enable_private(bot, *, tools=None, cooldown=0):
    agent = bot.plugins.get('agent')
    await agent.settings.update({
        'private_enabled': True,
        'cooldown_seconds': cooldown,
        'private_allowed_tools': list(tools or []),
    })


async def dispatch(bot, payload, connection=None):
    connection = connection or FakeConnection()
    await bot.dispatcher.dispatch(parse_event(payload), connection)
    return connection


async def trigger_reasons(bot, limit=10):
    rows = await (await bot.storage.db.execute(
        'SELECT outcome,reason,trigger_kind FROM trigger_decisions ORDER BY rowid DESC LIMIT ?', (limit,))).fetchall()
    return [(row['outcome'], row['reason'], row['trigger_kind']) for row in rows]


async def test_private_disabled_never_requests_model_or_saves_history(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM)
    try:
        complete = AsyncMock(return_value=reply('不应出现'))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = await dispatch(bot, private_message('你好', message_id=11))
        assert complete.await_count == 0
        assert not connection.sent
        assert await trigger_reasons(bot) == [('ignore', 'private_disabled', 'private')]
        assert not (await bot.storage.list_turns())['items']
        assert not await bot.storage.recent_chat('private', 30003, self_id=10001)
    finally:
        await bot.stop()


async def test_private_enabled_replies_in_private_scene_with_private_history(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM)
    try:
        await enable_private(bot)
        complete = AsyncMock(return_value=reply('私聊回复'))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        await dispatch(bot, private_message('你好', message_id=11), connection)
        assert complete.await_count == 1
        request = complete.await_args_list[0]
        assert request.kwargs['scene'] == 'private' and request.kwargs['scene_id'] == 30003
        assert request.args[0][-1]['role'] == 'user' and request.args[0][-1]['content'] == '你好'
        assert connection.sent and connection.sent[0][0] == 'private' and connection.sent[0][1] == 30003
        assert connection.texts() == ['私聊回复']
        assert ('accept', 'private_trigger_matched', 'private') in await trigger_reasons(bot)
        rows = await bot.storage.recent_chat('private', 30003, self_id=10001)
        assert [(row.role, row.content) for row in rows] == [('user', '你好'), ('assistant', '私聊回复')]
        assert not await bot.storage.recent_chat('group', 30003, self_id=10001)
        assert await bot.storage.kv_get('agent-consent-start', '10001:private:30003') is not None
        turns = await bot.storage.list_turns(scene='private', scene_id=30003)
        assert len(turns['items']) == 1 and turns['items'][0]['scene'] == 'private'
    finally:
        await bot.stop()


@pytest.mark.parametrize('text', ['/help', '   '])
async def test_private_commands_and_empty_messages_are_ignored(tmp_path, monkeypatch, text):
    bot = await make_bot(tmp_path, llm=LLM)
    try:
        await enable_private(bot)
        complete = AsyncMock(return_value=reply('不应出现'))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        await dispatch(bot, private_message(text, message_id=12))
        assert complete.await_count == 0
        if text.strip():
            # 命令由命令处理器消费，智能体不参与；只要不请求模型即可。
            return
        reasons = {reason for _, reason, _ in await trigger_reasons(bot)}
        assert reasons & {'command_or_empty', 'not_agent_message'}
    finally:
        await bot.stop()


async def test_private_tools_use_private_whitelist_only(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM)
    try:
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {
            'auto_group': True, 'trigger_modes': ['all'], 'cooldown_seconds': 0,
            'allowed_tools': ['basic__check_online']}})
        await enable_private(bot, tools=[])
        complete = AsyncMock(return_value=reply('ok'))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        await dispatch(bot, private_message('私聊无工具', message_id=11), connection)
        assert complete.await_args_list[-1].kwargs['tools'] is None
        # 群聊白名单不受私聊白名单影响。
        await dispatch(bot, group_message('群聊工具', message_id=12), connection)
        group_tools = [item['function']['name'] for item in complete.await_args_list[-1].kwargs['tools']]
        assert 'basic__check_online' in group_tools and 'agent__stay_silent' in group_tools
        await enable_private(bot, tools=['basic__check_online'])
        await dispatch(bot, private_message('私聊有工具', message_id=13), connection)
        private_tools = [item['function']['name'] for item in complete.await_args_list[-1].kwargs['tools']]
        assert private_tools == ['basic__check_online']
        # 插件全局停用后，即使私聊白名单勾选也不可见。
        await bot.permissions.set_plugin_enabled('basic', False)
        await dispatch(bot, private_message('插件已停用', message_id=14), connection)
        assert complete.await_args_list[-1].kwargs['tools'] is None
    finally:
        await bot.stop()


async def test_private_write_tool_is_blocked_by_global_sandbox_not_group_override(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM)
    try:
        # 群级沙盒放宽不应作用于私聊。
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {
            'auto_group': True, 'trigger_modes': ['all'], 'cooldown_seconds': 0,
            'sandbox_write': True, 'allowed_tools': ['workspace__write_file']}})
        await enable_private(bot, tools=['workspace__write_file'])
        complete = AsyncMock(side_effect=[
            reply(calls=[tool_call('workspace__write_file', '{"path":"note.txt","content":"hello"}')]),
            reply('写入被拒绝'),
        ])
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = await dispatch(bot, private_message('写文件', message_id=11))
        assert complete.await_count == 2
        tool_messages = [item for item in complete.await_args_list[1].args[0] if item.get('role') == 'tool']
        assert len(tool_messages) == 1
        blocked = json.loads(tool_messages[0]['content'])
        assert blocked['error'] == 'tool_blocked' and blocked['reason'] == 'write_disabled'
        assert not (tmp_path / 'data' / 'agent_workspace' / 'private_30003' / 'note.txt').exists()
        assert connection.texts() == ['写入被拒绝']
    finally:
        await bot.stop()


async def test_private_qq_send_tool_is_blocked_by_global_sandbox(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM, overrides={'sandbox': {'allow_qq_send': False}})
    try:
        await enable_private(bot, tools=['text__send_markdown_file'])
        complete = AsyncMock(side_effect=[
            reply(calls=[tool_call('text__send_markdown_file', '{"text":"DOC"}')]),
            reply('发送被拒绝'),
        ])
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = await dispatch(bot, private_message('发文件', message_id=11))
        assert complete.await_count == 2
        tool_messages = [item for item in complete.await_args_list[1].args[0] if item.get('role') == 'tool']
        blocked = json.loads(tool_messages[0]['content'])
        assert blocked['error'] == 'tool_blocked' and blocked['reason'] == 'qq_send_disabled'
        assert 'file' not in str(connection.sent)
        assert connection.texts() == ['发送被拒绝']
    finally:
        await bot.stop()


async def test_private_and_group_context_never_leak_into_each_other(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM)
    try:
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {
            'auto_group': True, 'trigger_modes': ['all'], 'cooldown_seconds': 0}})
        await enable_private(bot)
        complete = AsyncMock(return_value=reply('收到'))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        await dispatch(bot, group_message('GROUP_MARKER', message_id=11), connection)
        await dispatch(bot, private_message('PRIVATE_MARKER', message_id=12), connection)
        await dispatch(bot, private_message('PRIVATE_FOLLOWUP', message_id=13), connection)
        await dispatch(bot, group_message('GROUP_FOLLOWUP', message_id=14), connection)
        assert complete.await_count == 4
        first_group, first_private, second_private, second_group = [
            json.dumps(call.args[0], ensure_ascii=False) for call in complete.await_args_list]
        assert 'GROUP_MARKER' in first_group and 'PRIVATE_MARKER' not in first_group
        assert 'PRIVATE_MARKER' in first_private and 'GROUP_MARKER' not in first_private
        assert 'PRIVATE_MARKER' in second_private and 'GROUP_MARKER' not in second_private
        assert 'GROUP_MARKER' in second_group and 'PRIVATE_MARKER' not in second_group
        assert [row.content for row in await bot.storage.recent_chat('private', 30003, self_id=10001)
                if row.role == 'user'] == ['PRIVATE_MARKER', 'PRIVATE_FOLLOWUP']
        assert 'PRIVATE_MARKER' not in [
            row.content for row in await bot.storage.recent_chat('group', 20002, self_id=10001)]
    finally:
        await bot.stop()


async def test_private_cooldown_is_per_user(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM)
    try:
        await enable_private(bot, cooldown=60)
        complete = AsyncMock(return_value=reply('收到'))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        await dispatch(bot, private_message('第一条', message_id=11), connection)
        await dispatch(bot, private_message('冷却中', message_id=12), connection)
        assert complete.await_count == 1
        assert (await trigger_reasons(bot))[0] == ('ignore', 'agent_cooldown', 'private')
        other = private_message('另一位用户', message_id=13, user_id=40004)
        await dispatch(bot, other, connection)
        assert complete.await_count == 2
    finally:
        await bot.stop()


async def test_private_task_steering_and_cancel(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM, overrides={'plugin': {'max_concurrency': 1}})
    try:
        await enable_private(bot)
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def complete(messages, **options):
            requests.append([dict(item) for item in messages])
            entered.set()
            if len(requests) == 1:
                await release.wait()
            return reply(f'answer {len(requests)}')

        monkeypatch.setattr(bot.chat, 'complete', complete)

        async def settle():
            await asyncio.wait_for(bot.dispatcher._queue.join(), 3)
            if bot.agent_tasks.acknowledgements:
                await asyncio.wait_for(asyncio.gather(*bot.agent_tasks.acknowledgements), 2)

        connection = FakeConnection()
        try:
            await bot.on_raw_event(connection, private_message('original request', message_id=1))
            await asyncio.wait_for(entered.wait(), 2)
            active = next(iter(bot.agent_tasks.active.values()))
            await bot.on_raw_event(connection, private_message('补充：只分析配置文件', message_id=2))
            assert [item['content'] for item in active.pending] == ['只分析配置文件']
            release.set()
            await settle()
            assert len(requests) == 2
            assert any(item.get('content') == '只分析配置文件' for item in requests[1])
            assert len((await bot.storage.list_turns(scene='private', scene_id=30003))['items']) == 1
        finally:
            release.set()

        # 第二次任务用取消收尾。
        entered.clear()
        release = asyncio.Event()
        await bot.on_raw_event(connection, private_message('second request', message_id=3))
        await asyncio.wait_for(entered.wait(), 2)
        await bot.on_raw_event(connection, private_message('不用做了', message_id=4))
        await settle()
        assert not bot.agent_tasks.active
        turns = (await bot.storage.list_turns(scene='private', scene_id=30003))['items']
        assert len(turns) == 2
        cancelled = [await bot.storage.turn_detail(item['turn_id']) for item in turns]
        assert any(detail['status'] == 'cancelled' and detail['reason'] == 'user_cancelled'
                   for detail in cancelled)
    finally:
        await bot.stop()


async def test_private_control_requires_running_private_task(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM)
    try:
        await enable_private(bot)
        complete = AsyncMock(return_value=reply('不应出现'))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        await bot.on_raw_event(connection, private_message('/任务进度', message_id=11))
        await asyncio.wait_for(bot.dispatcher._queue.join(), 3)
        if bot.agent_tasks.acknowledgements:
            await asyncio.wait_for(asyncio.gather(*bot.agent_tasks.acknowledgements), 2)
        assert complete.await_count == 0
        assert connection.texts() == ['你在当前会话没有正在执行的智能体任务。']
    finally:
        await bot.stop()


async def test_private_long_reply_uses_private_forward(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM, overrides={'chat': {
        'reply_mode': 'forward', 'forward_threshold': 200, 'reply_chunk_size': 80, 'reply_interval_ms': 0}})
    try:
        await enable_private(bot)
        text = '私聊长回复内容' * 40
        complete = AsyncMock(return_value=reply(text))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        connection.send_private_forward_msg = AsyncMock(return_value={'message_id': 31})
        await dispatch(bot, private_message('讲个长故事', message_id=11), connection)
        assert connection.send_private_forward_msg.await_count == 1
        assert not connection.sent
    finally:
        await bot.stop()


async def test_private_access_and_model_switch_respected(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM, overrides={'permission': {'allow_private': False}})
    try:
        await enable_private(bot)
        complete = AsyncMock(return_value=reply('不应出现'))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        await dispatch(bot, private_message('被拒绝', message_id=11))
        assert complete.await_count == 0
        assert not (await bot.storage.list_turns())['items']
        await bot.update_config({'permission': {'allow_private': True}, 'llm': {'enable_private': False}})
        await dispatch(bot, private_message('模型私聊关闭', message_id=12))
        assert complete.await_count == 0
        assert ('reject', 'scene_ai_disabled', 'private') in await trigger_reasons(bot)
    finally:
        await bot.stop()
