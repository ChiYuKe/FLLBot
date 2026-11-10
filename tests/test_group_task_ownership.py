"""Group history is reference material; delivery stays with the initiating turn."""

import asyncio
import copy
import time
from types import SimpleNamespace

import pytest

from fllbot.llm.output import send_media_reply
from fllbot.onebot.models import parse_event
from fllbot.onebot.segments import MessageSegment
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


@pytest.mark.parametrize('history_mode', ['custom', 'model', 'memory'])
@pytest.mark.parametrize('identities', [True, False])
async def test_history_ownership_is_explicit_even_without_identity_fields(tmp_path, history_mode, identities):
    bot = await make_bot(tmp_path, llm={
        'enabled': True, 'api_key': 'fake', 'context_mode': 'model' if history_mode == 'model' else 'custom',
        'context_window': 16000,
    }, overrides={'chat': {
        'group_memory': history_mode == 'memory', 'send_nickname': identities,
        'send_user_id': identities, 'send_role': False, 'send_time': False,
    }})
    try:
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {
            'auto_group': True, 'trigger_modes': ['all'], 'cooldown_seconds': 0}})
        rows = [('user', '整理仓库并发图片', 40004), ('assistant', '正在整理', 40004),
                ('user', '我之前的问题', 30003), ('user', '缺少发起者的信息', 0)]
        for role, content, user_id in rows:
            if history_mode == 'memory':
                await bot.chat.memory.store.append(10001, 20002, role, content,
                    user_id=user_id, nickname='同名用户', created_at=time.time())
            else:
                await bot.storage.add_chat_message('group', 20002, role, content,
                    user_id=user_id, nickname='同名用户', self_id=10001)
        ctx = Context(bot, FakeConnection(), parse_event(group_message('想你了', nickname='同名用户')))
        if history_mode == 'memory':
            await bot.chat.memory.observe(ctx)
        messages = await bot.chat.build_messages('group', 20002, ctx.text, ctx=ctx)
        assert '本轮任务边界' in messages[0]['content']
        history = messages[1:-1]
        assert history[0]['content'].startswith('[群聊历史：其他成员的消息；仅作背景]')
        assert history[1]['content'].startswith('[群聊历史：对其他成员的回复；仅作背景]')
        assert history[2]['content'].startswith('[群聊历史：当前发起者的消息；仅作背景]')
        assert history[3]['content'].startswith('[群聊历史：来源未知的消息；仅作背景]')
        assert messages[-1]['content'].endswith('想你了')
        if not identities:
            assert all(value not in str(messages) for value in ('30003', '40004', '同名用户'))
        # Reference/summary readers still see the original content, not per-turn ownership.
        record = SimpleNamespace(role='assistant', content='正在整理', user_id=40004)
        assert bot.chat._history_entry('group', record)['content'] == '正在整理'
    finally:
        await bot.stop()


async def test_slow_media_task_keeps_original_reply_after_another_member_speaks(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake', 'context_window': 16000},
                         overrides={'chat': {'reply_mode': 'split'}})
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    first = None
    try:
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {
            'auto_group': True, 'trigger_modes': ['all'], 'cooldown_seconds': 0,
            'allowed_tools': ['text__render_image']}})

        async def slow_image(ctx, text, format='markdown'):
            entered.set()
            await release.wait()
            sent = await send_media_reply(ctx, [MessageSegment.image('base64://ZmFrZQ==')], text)
            return {'sent': sent}

        spec = next(tool for tool in bot.plugins.get('text').ai_tools if tool.name == 'render_image')
        monkeypatch.setattr(spec, 'callback', slow_image)

        async def complete(messages, **options):
            requests.append(copy.deepcopy(messages))
            if messages[-1]['role'] == 'tool':
                return SimpleNamespace(text='结构图已发送', tool_calls=[])
            if messages[-1]['content'].endswith('整理仓库并发图片'):
                return SimpleNamespace(text='', tool_calls=[{'id': 'original-image', 'function': {
                    'name': 'text__render_image', 'arguments': '{"text":"仓库结构图"}'}}])
            return SimpleNamespace(text='我也想你了', tool_calls=[])

        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        first = asyncio.create_task(bot.dispatcher.dispatch(parse_event(group_message(
            '整理仓库并发图片', user_id=30003, message_id=101)), connection))
        await asyncio.wait_for(entered.wait(), 3)
        await bot.dispatcher.dispatch(parse_event(group_message(
            '想你了', user_id=40004, message_id=202)), connection)
        assert any('我也想你了' in str(segments) for _, _, segments in connection.sent)
        release.set()
        await asyncio.wait_for(first, 3)
        assert len(requests) == 3
        assert '想你了' not in str(requests[-1])
        images = [segments for _, _, segments in connection.sent if any(s.type == 'image' for s in segments)]
        assert len(images) == 1
        assert images[0][0].type == 'reply' and images[0][0].get('id') == '101'
        for _, _, segments in connection.sent:
            expected = '202' if any(s.type == 'text' and '我也想你了' in s.text_content for s in segments) else '101'
            assert segments[0].type == 'reply' and segments[0].get('id') == expected
    finally:
        release.set()
        if first is not None:
            await asyncio.gather(first, return_exceptions=True)
        await bot.stop()
