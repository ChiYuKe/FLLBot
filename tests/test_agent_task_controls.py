import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from helpers import FakeConnection, group_message, make_bot


@pytest.fixture
async def running_bot(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake', 'context_window': 16000},
                         overrides={'plugin': {'max_concurrency': 1}})
    await bot.permissions.save_group(20002, enabled=True, config={'policy': {
        'auto_group': True, 'trigger_modes': ['all'], 'cooldown_seconds': 0,
        'allowed_tools': ['basic__check_online']}})
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def complete(messages, **options):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            entered.set()
            await release.wait()
        return SimpleNamespace(text='answer', tool_calls=[])
    monkeypatch.setattr(bot.chat, 'complete', complete)
    connection = FakeConnection()
    try:
        await bot.on_raw_event(connection, group_message('original request', message_id=1))
        await asyncio.wait_for(entered.wait(), 2)
        yield bot, connection, requests, release
    finally:
        release.set()
        await bot.stop()


async def settle(bot):
    await asyncio.wait_for(bot.dispatcher._queue.join(), 3)
    if bot.agent_tasks.acknowledgements:
        await asyncio.wait_for(asyncio.gather(*bot.agent_tasks.acknowledgements), 2)


async def test_raw_cancel_bypasses_single_busy_worker_and_preserves_progress(running_bot):
    bot, connection, requests, _ = running_bot
    active = next(iter(bot.agent_tasks.active.values()))
    await asyncio.wait_for(bot.on_raw_event(connection, group_message('不用做了', message_id=2)), 1)
    await settle(bot)
    assert len(requests) == 1
    assert not bot.agent_tasks.active
    detail = await bot.storage.turn_detail(active.ctx.turn_id)
    assert detail['status'] == 'cancelled' and detail['reason'] == 'user_cancelled'
    assert any(item['phase'] == 'model' for item in detail['progress'])
    assert any(item['status'] == 'cancelled' for item in detail['progress'])
    assert 'original request' not in str(detail['progress'])


async def test_raw_steering_applies_to_next_step_and_is_not_a_second_task(running_bot):
    bot, connection, requests, release = running_bot
    await bot.on_raw_event(connection, group_message('补充：只分析配置文件', message_id=2))
    await bot.on_raw_event(connection, group_message('/任务进度', message_id=3))
    release.set()
    await settle(bot)
    assert len(requests) == 2
    assert any(item.get('content') == '只分析配置文件' for item in requests[1])
    turns = await bot.storage.list_turns()
    assert len(turns['items']) == 1
    detail = await bot.storage.turn_detail(turns['items'][0]['turn_id'])
    assert any(item['phase'] == 'steering' for item in detail['progress'])
    assert '只分析配置文件' not in str(detail['progress'])


async def test_other_sender_or_account_cannot_cancel_task(running_bot):
    bot, connection, _, release = running_bot
    active = next(iter(bot.agent_tasks.active.values()))
    other = group_message('/任务取消', message_id=2)
    other['user_id'] = 54321
    await bot.on_raw_event(connection, other)
    wrong_account = group_message('/任务取消', message_id=3)
    wrong_account['self_id'] = 22
    await bot.on_raw_event(FakeConnection(self_id=22), wrong_account)
    assert bot.conversations.can_cancel(active.ctx.turn_id)
    assert not active.ctx.cancellation_reason
    release.set()
    await settle(bot)


async def test_duplicate_supplement_is_consumed_once(running_bot):
    bot, connection, requests, release = running_bot
    payload = group_message('/任务补充 keep this', message_id=2)
    await bot.on_raw_event(connection, payload)
    await bot.on_raw_event(connection, payload)
    release.set()
    await settle(bot)
    assert sum(item.get('content') == 'keep this' for item in requests[-1]) == 1


async def test_control_acknowledgement_does_not_block_protocol_reader(running_bot, monkeypatch):
    bot, connection, _, release = running_bot
    sending, sent = asyncio.Event(), asyncio.Event()
    async def send(*args, **kwargs):
        sending.set()
        await sent.wait()
        return {'message_id': 100}
    monkeypatch.setattr(connection, 'send_group_msg', send)
    await asyncio.wait_for(bot.on_raw_event(connection, group_message('/任务进度', message_id=2)), 1)
    await asyncio.wait_for(sending.wait(), 1)
    sent.set()
    release.set()
    await settle(bot)


async def test_expired_task_rejects_steering_and_no_stale_content_is_saved(running_bot):
    bot, connection, _, release = running_bot
    active = next(iter(bot.agent_tasks.active.values()))
    await bot.storage.clear_chat('group', 20002, self_id=connection.self_id)
    await bot.on_raw_event(connection, group_message('/任务补充 stale', message_id=2))
    assert not active.pending
    release.set()
    await settle(bot)


async def test_controls_stop_accepting_when_result_delivery_begins(running_bot):
    bot, connection, _, release = running_bot
    active = next(iter(bot.agent_tasks.active.values()))
    bot.agent_tasks.finish_input(active.ctx)
    await bot.on_raw_event(connection, group_message('补充：too late', message_id=2))
    assert not active.pending
    release.set()
    await settle(bot)


async def test_cancel_during_external_tool_preserves_uncertain_fact_and_blocks_late_send(tmp_path, monkeypatch):
    directory = tmp_path / 'plugins'
    directory.mkdir()
    (directory / 'slow.py').write_text('''import asyncio
from fllbot.plugin import Plugin
plugin = Plugin('slow', enabled_by_default=True)
entered, release = asyncio.Event(), asyncio.Event()
@plugin.on_ai_tool('send', description='slow external action', parameters={'type':'object'})
async def send(ctx):
    entered.set()
    try:
        await release.wait()
    except asyncio.CancelledError:
        await release.wait()
    await ctx.reply('LATE_SEND_MUST_NOT_HAPPEN')
    return {'done': True}
''', encoding='utf-8')
    bot = await make_bot(tmp_path, plugin_dirs=[directory], llm={
        'enabled': True, 'api_key': 'fake', 'context_window': 16000})
    await bot.permissions.save_group(20002, enabled=True, config={'policy': {
        'auto_group': True, 'trigger_modes': ['all'], 'cooldown_seconds': 0,
        'allowed_tools': ['slow__send']}})
    complete = AsyncMock(return_value=SimpleNamespace(text='', tool_calls=[{
        'id': 'send-once', 'function': {'name': 'slow__send', 'arguments': '{}'}}]))
    monkeypatch.setattr(bot.chat, 'complete', complete)
    owner = bot.plugins.get('slow')
    module = owner.ai_tools[0].callback.__globals__
    connection = FakeConnection()
    try:
        await bot.on_raw_event(connection, group_message('send', message_id=1))
        await asyncio.wait_for(module['entered'].wait(), 2)
        turn_id = next(iter(bot.agent_tasks.active.values())).ctx.turn_id
        await bot.on_raw_event(connection, group_message('/任务取消', message_id=2))
        await settle(bot)
        module['release'].set()
        for task in list(bot.tool_executor._callbacks):
            await asyncio.gather(task, return_exceptions=True)
        detail = await bot.storage.turn_detail(turn_id)
        assert detail['status'] == 'cancelled'
        assert detail['tools'][0]['status'] == 'uncertain'
        assert 'LATE_SEND_MUST_NOT_HAPPEN' not in str(connection.sent)
        complete.assert_awaited_once()
    finally:
        module['release'].set()
        await bot.stop()
