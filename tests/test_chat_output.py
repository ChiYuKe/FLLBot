from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from fllbot.llm.output import send_chat_reply
from fllbot.onebot.connection import ActionError
from fllbot.onebot.models import parse_event
from fllbot.onebot.segments import MessageSegment
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot, private_message


@pytest.mark.parametrize('mode', ['split', 'forward', 'media'])
@pytest.mark.parametrize('receipt', [None, {}, {'message_id': None}, {'message_id': True}, {'message_id': ''}, {'message_id': 0}])
async def test_unconfirmed_receipt_is_uncertain_and_never_visible_or_replayed(tmp_path, mode, receipt):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {'reply_mode': 'forward' if mode == 'forward' else 'split'}})
    connection = FakeConnection()
    connection.send_group_msg = AsyncMock(return_value=receipt)
    connection.send_group_forward_msg = AsyncMock(return_value=receipt)
    ctx = Context(bot, connection, parse_event(group_message('hello')))
    try:
        with pytest.raises(RuntimeError, match='message_id'):
            async with bot.conversations.turn(ctx):
                if mode == 'media':
                    await bot.deliveries.send_media(ctx, [MessageSegment.record('test.wav')], 'spoken')
                else:
                    await send_chat_reply(ctx, 'candidate')
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert detail['status'] == 'delivery_uncertain'
        rows = await (await bot.storage.db.execute('SELECT status,message_id FROM delivery_parts ORDER BY part_index')).fetchall()
        assert tuple(rows[0]) == ('uncertain', '')
        assert not await bot.storage.recent_chat('group', 20002, self_id=10001)
        assert not await bot.storage.recovery_plan(ctx.turn_id)
        assert connection.send_group_msg.await_count == int(mode != 'forward')
        assert connection.send_group_forward_msg.await_count == int(mode == 'forward')
    finally:
        await bot.stop()


@pytest.mark.parametrize('private', [False, True])
async def test_long_replies_use_forward_nodes(tmp_path: Path, private):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'forward_threshold': 200, 'reply_chunk_size': 100, 'reply_interval_ms': 0,
    }})
    try:
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock(return_value={'message_id': 'forward-1'})
        connection.send_private_forward_msg = AsyncMock(return_value={'message_id': 'forward-2'})
        payload = private_message('你好') if private else group_message('你好')
        ctx = Context(bot=bot, connection=connection, event=parse_event(payload))
        text = '长回复内容' * 80
        await send_chat_reply(ctx, text)
        target = connection.send_private_forward_msg if private else connection.send_group_forward_msg
        target.assert_awaited_once()
        nodes = target.call_args.args[1]
        assert all(node['type'] == 'node' for node in nodes)
        assert all(node['data']['user_id'] == str(ctx.self_id) for node in nodes)
        assert ''.join(n['data']['content'][0]['data']['text'] for n in nodes) == text
        assert not connection.sent
    finally:
        await bot.stop()


async def test_forward_failure_falls_back_without_losing_reply(tmp_path: Path):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': 'forward', 'reply_chunk_size': 50, 'reply_interval_ms': 0,
        'quote_reply': True, 'at_sender': True,
    }})
    try:
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock(side_effect=ActionError('send_group_forward_msg', retcode=1404, message='unsupported'))
        ctx = Context(bot=bot, connection=connection, event=parse_event(group_message('你好')))
        text = '测试回复' * 50
        await send_chat_reply(ctx, text)
        sent = [segment for _, _, segments in connection.sent for segment in segments]
        assert sum(segment.type == 'reply' for segment in sent) == 1
        assert sum(segment.type == 'at' for segment in sent) == 1
        assert ''.join(segment.text_content for segment in sent if segment.is_text).replace(' ', '') == text
    finally:
        await bot.stop()


@pytest.mark.parametrize('mode,length,expected', [('auto', 20, False), ('split', 300, False), ('forward', 20, True)])
async def test_short_and_explicit_output_modes(tmp_path: Path, mode, length, expected):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': mode, 'forward_threshold': 200, 'reply_interval_ms': 0,
    }})
    try:
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock(return_value={'message_id': 'forward-1'})
        ctx = Context(bot=bot, connection=connection, event=parse_event(group_message('你好')))
        await send_chat_reply(ctx, '文' * length)
        assert bool(connection.send_group_forward_msg.await_count) is expected
        assert bool(connection.sent) is not expected
    finally:
        await bot.stop()


async def test_forward_timeout_is_not_retried_or_visible(tmp_path: Path):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {'reply_mode': 'forward'}})
    try:
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock(side_effect=ActionError('send_group_forward_msg', message='timeout'))
        ctx = Context(bot=bot, connection=connection, event=parse_event(group_message('hello')))
        with pytest.raises(ActionError):
            await send_chat_reply(ctx, 'candidate')
        assert not connection.sent
        assert not await bot.storage.recent_chat('group', 20002, self_id=10001)
        rows = await (await bot.storage.db.execute('SELECT status FROM delivery_parts ORDER BY part_index')).fetchall()
        assert rows[0]['status'] == 'uncertain'
    finally:
        await bot.stop()


async def test_partial_delivery_projects_only_sent_part(tmp_path: Path):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': 'split', 'reply_chunk_size': 50, 'reply_interval_ms': 0}})
    try:
        connection = FakeConnection()
        connection.send_group_msg = AsyncMock(side_effect=[{'message_id': 99}, ActionError('send_group_msg', retcode=100)])
        ctx = Context(bot=bot, connection=connection, event=parse_event(group_message('hello')))
        with pytest.raises(ActionError):
            await send_chat_reply(ctx, 'x' * 100)
        rows = await bot.storage.recent_chat('group', 20002, self_id=10001)
        assert [row.content for row in rows] == ['x' * 50]
        parts = await (await bot.storage.db.execute('SELECT status,message_id FROM delivery_parts ORDER BY part_index')).fetchall()
        assert [(row['status'], row['message_id']) for row in parts] == [('sent', '99'), ('failed', '')]
    finally:
        await bot.stop()


async def test_group_disabled_between_parts_stops_delivery(tmp_path: Path):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': 'split', 'reply_chunk_size': 50, 'reply_interval_ms': 0}})
    try:
        connection = FakeConnection()
        async def first_part(*args, **kwargs):
            await bot.permissions.set_group_enabled(20002, False)
            return {'message_id': 1}
        connection.send_group_msg = AsyncMock(side_effect=first_part)
        ctx = Context(bot=bot, connection=connection, event=parse_event(group_message('hello')))
        await send_chat_reply(ctx, 'x' * 100)
        assert connection.send_group_msg.await_count == 1
        rows = await bot.storage.recent_chat('group', 20002, self_id=10001)
        assert [row.content for row in rows] == ['x' * 50]
        statuses = await (await bot.storage.db.execute('SELECT status FROM delivery_parts ORDER BY part_index')).fetchall()
        assert [row['status'] for row in statuses] == ['sent', 'cancelled']
    finally:
        await bot.stop()
