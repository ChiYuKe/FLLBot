import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from fllbot.config import LLMConfig, PluginConfig
from fllbot.onebot.models import parse_event
from fllbot.onebot.segments import MessageSegment
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


def payload(index, user=None, group=20002):
    result = group_message(f'REQUEST_{index}', message_id=index, user_id=user or index + 30000, at=10001)
    result['group_id'] = group
    return result


async def setup_group(bot, group=20002):
    await bot.permissions.save_group(group, enabled=True, config={'policy': {
        'auto_group': True, 'trigger_modes': ['at'], 'cooldown_seconds': 0}})


def test_default_parallelism_and_explicit_provider_limits():
    assert PluginConfig().max_concurrency == PluginConfig().max_concurrent_per_group == 6
    config = LLMConfig(providers={'custom': {'max_concurrency': 2}})
    assert config.max_concurrency == 6 and config.resolve('custom')[1].max_concurrency == 2
    assert LLMConfig().resolve()[1].max_concurrency == 6


async def test_six_group_members_reach_real_model_concurrently_and_replies_are_targeted(tmp_path):
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake', 'context_window': 16000})
    await setup_group(bot)
    entered = {i: asyncio.Event() for i in range(1, 9)}
    release = {i: asyncio.Event() for i in entered}
    active = set()
    maximum = 0
    async def model(request):
        nonlocal maximum
        data = json.loads(request.content)
        index = int(re.search(r'REQUEST_(\d+)', data['messages'][-1]['content'])[1])
        active.add(index)
        maximum = max(maximum, len(active))
        entered[index].set()
        await release[index].wait()
        active.remove(index)
        return httpx.Response(200, json={'choices': [{'message': {'content': f'ANSWER_{index}'}, 'finish_reason': 'stop'}]})
    bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(model))
    connection = FakeConnection()
    try:
        for index in range(1, 8):
            await bot.on_raw_event(connection, payload(index))
        # 同一发起者的第二条 @ 消息现在按“追问”并入原任务；这里换一位成员验证
        # 第 7 个并发任务仍要等本群并发额度释放。
        await bot.on_raw_event(connection, payload(8, user=30009))
        await asyncio.wait_for(asyncio.gather(*(entered[i].wait() for i in range(1, 7))), 3)
        assert maximum == 6 and len(bot.agent_tasks.active) == 6
        assert not entered[7].is_set() and not entered[8].is_set()
        release[3].set()
        await asyncio.wait_for(entered[7].wait(), 3)
        assert not entered[8].is_set()
        release[1].set()
        await asyncio.wait_for(entered[8].wait(), 3)
        for event in release.values():
            event.set()
        await asyncio.wait_for(bot.dispatcher._queue.join(), 5)
        assert maximum == 6
        answers = []
        for kind, sent in connection.calls:
            if kind == 'send_group_msg':
                segments = [item.to_dict() for item in sent['message']]
                text = ''.join(item['data'].get('text', '') for item in segments)
                index = int(re.search(r'ANSWER_(\d+)', text)[1])
                assert segments[0] == {'type': 'reply', 'data': {'id': str(index)}}
                answers.append(index)
        assert sorted(answers) == list(range(1, 9))
        history = await bot.storage.recent_chat('group', 20002, self_id=connection.self_id)
        for row in history:
            if row.role == 'assistant':
                index = int(re.search(r'ANSWER_(\d+)', row.content)[1])
                assert row.user_id == (30009 if index == 8 else 30000 + index)
                # 回复对象只作为元数据保存；正文里既不带昵称也不带 QQ 号，免得模型学着在回答里报出来。
                entry = bot.chat._history_entry('group', row)['content']
                assert entry == row.content
        await bot.update_config({'chat': {'send_user_id': False, 'send_nickname': False}})
        for row in history:
            if row.role == 'assistant':
                assert str(row.user_id) not in bot.chat._history_entry('group', row)['content']
        assert bot.capacity.used == 0 and not bot.agent_tasks.active
    finally:
        for event in release.values():
            event.set()
        await bot.stop()


async def test_group_limit_leaves_workers_for_other_groups(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, builtin=False, overrides={
        'plugin': {'max_concurrency': 10, 'max_concurrent_per_group': 6}})
    entered, release = set(), asyncio.Event()
    ready = asyncio.Event()
    async def dispatch(event, *args, **kwargs):
        entered.add((event.group_id, event.user_id))
        if len(entered) == 7:
            ready.set()
        await release.wait()
    monkeypatch.setattr(bot.dispatcher, '_dispatch_guarded', dispatch)
    try:
        for index in range(1, 8):
            await bot.dispatcher.enqueue(parse_event(payload(index)), FakeConnection())
        await bot.dispatcher.enqueue(parse_event(payload(8, group=20003)), FakeConnection())
        await asyncio.wait_for(ready.wait(), 2)
        assert len([item for item in entered if item[0] == 20002]) == 6
        assert (20003, 30008) in entered and (20002, 30007) not in entered
        release.set()
        await asyncio.wait_for(bot.dispatcher._queue.join(), 2)
        assert (20002, 30007) in entered
    finally:
        release.set()
        await bot.stop()


async def test_task_targeting_handles_media_fallback_and_other_destinations(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    ctx = Context(bot, FakeConnection(), parse_event(payload(1)))
    ctx.turn_input = 'task'
    try:
        media = [MessageSegment.image('https://example.test/image.png')]
        assert ctx.targeted_segments(media)[0].type == 'reply'
        assert len(ctx.targeted_segments(ctx.targeted_segments(media))) == 2
        assert ctx.targeted_segments(media, group_id=999) == media
        assert ctx.targeted_segments(media, user_id=999) == media
        ctx.event.message_id = 0
        assert ctx.targeted_segments(media)[0].data['qq'] == '30001'
    finally:
        await bot.stop()


async def test_concurrent_consent_initialization_uses_one_cutoff(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        results = await asyncio.gather(*(bot.storage.kv_initialize('agent-consent-start', 'account:group', i) for i in range(6)))
        assert len(set(results)) == 1
    finally:
        await bot.stop()


async def test_parallel_members_supplements_and_cancellation_stay_with_sender(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake', 'context_window': 16000})
    await setup_group(bot)
    entered = {1: asyncio.Event(), 2: asyncio.Event()}
    release = asyncio.Event()
    captured = []
    async def model(messages, **options):
        current = messages[-1]['content']
        captured.append(current)
        if 'REQUEST_1' in current:
            entered[1].set()
            await release.wait()
        elif 'REQUEST_2' in current:
            entered[2].set()
            await asyncio.Event().wait()
        return SimpleNamespace(text='done', tool_calls=[])
    monkeypatch.setattr(bot.chat, 'complete', AsyncMock(side_effect=model))
    connection = FakeConnection()
    try:
        for i in (1, 2):
            await bot.on_raw_event(connection, payload(i))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
        turns = {state.ctx.user_id: state.ctx.turn_id for state in bot.agent_tasks.active.values()}
        await bot.on_raw_event(connection, group_message('补充：NEW_CONDITION', user_id=30001, message_id=3))
        assert len(bot.agent_tasks.active[(10001, 'group', 20002, 30001)].pending) == 1
        assert not bot.agent_tasks.active[(10001, 'group', 20002, 30002)].pending
        await bot.on_raw_event(connection, group_message('/任务取消', user_id=30002, message_id=4))
        assert bot.conversations.can_cancel(turns[30001])
        release.set()
        await asyncio.wait_for(bot.dispatcher._queue.join(), 3)
        assert 'NEW_CONDITION' in captured
        assert (await bot.storage.turn_detail(turns[30001]))['status'] == 'delivered'
        assert (await bot.storage.turn_detail(turns[30002]))['status'] == 'cancelled'
    finally:
        release.set()
        await bot.stop()
