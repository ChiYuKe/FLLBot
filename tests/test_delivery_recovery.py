import asyncio
from unittest.mock import AsyncMock

import pytest

from fllbot.llm.output import send_chat_reply
from fllbot.onebot.connection import ActionError, InvalidDeliveryReceipt
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


async def failed_reply(bot, connection, failure):
    ctx = Context(bot,connection,parse_event(group_message('hello')))
    connection.send_group_msg = AsyncMock(side_effect=[{'message_id':1},failure])
    with pytest.raises(ActionError):
        async with bot.conversations.turn(ctx):
            await send_chat_reply(ctx,'x'*100)
    return ctx


async def test_recovery_only_sends_known_unsent_parts(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,overrides={'chat':{'reply_mode':'split','reply_chunk_size':50,'reply_interval_ms':0}})
    connection = FakeConnection()
    bot.manager.add(connection)
    try:
        ctx = await failed_reply(bot,connection,ActionError('send_group_msg',retcode=100))
        assert await bot.storage.recovery_plan(ctx.turn_id)
        connection.send_group_msg = AsyncMock(return_value={'message_id':2})
        bot.chat.complete = AsyncMock(side_effect=AssertionError('must not generate'))
        result = await bot.deliveries.recover(ctx.turn_id)
        assert result['status'] == 'delivered'
        connection.send_group_msg.assert_awaited_once()
        assert connection.send_group_msg.call_args.args[1][0].text_content == 'x'*50
        rows = await bot.storage.recent_chat('group',20002,self_id=10001)
        assert [row.content for row in rows] == ['x'*50,'x'*50]
        assert [part['message_id'] for part in result['deliveries']] == ['1','2']
        with pytest.raises(ValueError):
            await bot.deliveries.recover(ctx.turn_id)
        connection.send_group_msg.assert_awaited_once()
    finally:
        await bot.stop()


async def test_uncertain_send_cannot_be_recovered(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,overrides={'chat':{'reply_mode':'split','reply_chunk_size':50,'reply_interval_ms':0}})
    connection = FakeConnection()
    bot.manager.add(connection)
    try:
        ctx = await failed_reply(bot,connection,ActionError('send_group_msg',message='timeout'))
        assert not await bot.storage.recovery_plan(ctx.turn_id)
        with pytest.raises(ValueError):
            await bot.deliveries.recover(ctx.turn_id)
        assert connection.send_group_msg.await_count == 2
    finally:
        await bot.stop()


async def test_recovery_missing_receipt_preserves_previous_part_and_blocks_another_attempt(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': 'split', 'reply_chunk_size': 50, 'reply_interval_ms': 0}})
    connection = FakeConnection()
    bot.manager.add(connection)
    try:
        ctx = await failed_reply(bot, connection, ActionError('send_group_msg', retcode=100))
        connection.send_group_msg = AsyncMock(return_value={})
        with pytest.raises(InvalidDeliveryReceipt):
            await bot.deliveries.recover(ctx.turn_id)
        assert (await bot.storage.turn_detail(ctx.turn_id))['status'] == 'delivery_uncertain'
        assert [row.content for row in await bot.storage.recent_chat('group', 20002, self_id=10001)] == ['x' * 50]
        assert not await bot.storage.recovery_plan(ctx.turn_id)
        with pytest.raises(ValueError):
            await bot.deliveries.recover(ctx.turn_id)
        connection.send_group_msg.assert_awaited_once()
    finally:
        await bot.stop()


async def test_recovery_denied_after_account_epoch_changes(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,overrides={'chat':{'reply_mode':'split','reply_chunk_size':50,'reply_interval_ms':0}})
    connection = FakeConnection()
    bot.manager.add(connection)
    try:
        ctx = await failed_reply(bot,connection,ActionError('send_group_msg',retcode=100))
        await bot.bots.update(10001,enabled=False)
        await bot.bots.update(10001,enabled=True)
        with pytest.raises(ValueError):
            await bot.deliveries.recover(ctx.turn_id)
        assert connection.send_group_msg.await_count == 2
    finally:
        await bot.stop()


async def test_permissions_revoked_before_recovery_block_send(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,overrides={'chat':{'reply_mode':'split','reply_chunk_size':50,'reply_interval_ms':0}})
    connection = FakeConnection()
    bot.manager.add(connection)
    try:
        ctx = await failed_reply(bot,connection,ActionError('send_group_msg',retcode=100))
        await bot.permissions.set_group_enabled(20002,False)
        with pytest.raises(ValueError):
            await bot.deliveries.recover(ctx.turn_id)
        assert connection.send_group_msg.await_count == 2
    finally:
        await bot.stop()


async def test_timeout_during_recovery_blocks_further_recovery(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,overrides={'chat':{'reply_mode':'split','reply_chunk_size':50,'reply_interval_ms':0}})
    connection = FakeConnection()
    bot.manager.add(connection)
    try:
        ctx = await failed_reply(bot,connection,ActionError('send_group_msg',retcode=100))
        connection.send_group_msg = AsyncMock(side_effect=ActionError('send_group_msg',message='timeout'))
        with pytest.raises(ActionError):
            await bot.deliveries.recover(ctx.turn_id)
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert detail['status'] == 'delivery_uncertain'
        assert [part['status'] for part in detail['deliveries']] == ['sent','uncertain']
        with pytest.raises(ValueError):
            await bot.deliveries.recover(ctx.turn_id)
        connection.send_group_msg.assert_awaited_once()
    finally:
        await bot.stop()


async def test_account_disable_cancels_recovery_but_preserves_uncertain_send(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,overrides={'chat':{'reply_mode':'split','reply_chunk_size':50,'reply_interval_ms':0}})
    connection = FakeConnection()
    bot.manager.add(connection)
    task = None
    try:
        ctx = await failed_reply(bot,connection,ActionError('send_group_msg',retcode=100))
        entered = asyncio.Event()
        async def waiting(*args,**kwargs):
            entered.set()
            await asyncio.Event().wait()
        connection.send_group_msg = AsyncMock(side_effect=waiting)
        task = asyncio.create_task(bot.deliveries.recover(ctx.turn_id))
        await asyncio.wait_for(entered.wait(),1)
        assert bot.conversations.can_cancel(ctx.turn_id)
        await bot.bots.update(10001,enabled=False)
        with pytest.raises(asyncio.CancelledError):
            await task
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert detail['status'] == 'delivery_uncertain'
        assert [part['status'] for part in detail['deliveries']] == ['sent','uncertain']
        assert not bot.conversations.can_cancel(ctx.turn_id)
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        await bot.stop()


async def test_recovery_preserves_quote_at_and_chunks_after_settings_change(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,overrides={'chat':{'reply_mode':'split','reply_chunk_size':50,
        'reply_interval_ms':0,'quote_reply':True,'at_sender':True}})
    connection = FakeConnection()
    bot.manager.add(connection)
    try:
        ctx = Context(bot,connection,parse_event(group_message('hello')))
        connection.send_group_msg = AsyncMock(side_effect=ActionError('send_group_msg',retcode=100))
        with pytest.raises(ActionError):
            async with bot.conversations.turn(ctx):
                await send_chat_reply(ctx,'x'*100)
        await bot.update_config({'chat':{'reply_mode':'forward','reply_chunk_size':20,'quote_reply':False,'at_sender':False}})
        connection.send_group_msg = AsyncMock(return_value={'message_id':2})
        await bot.deliveries.recover(ctx.turn_id)
        assert connection.send_group_msg.await_count == 2
        first = connection.send_group_msg.call_args_list[0].args[1]
        second = connection.send_group_msg.call_args_list[1].args[1]
        assert [segment.type for segment in first] == ['reply','at','text','text']
        assert first[0].get('id') == str(ctx.message_id)
        assert first[1].get('qq') == str(ctx.user_id)
        assert first[-1].text_content == 'x'*50
        assert [segment.type for segment in second] == ['text']
        assert second[0].text_content == 'x'*50
    finally:
        await bot.stop()


async def test_rejected_forward_recovers_only_failed_fallback_part(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,overrides={'chat':{'reply_mode':'forward','reply_chunk_size':50,'reply_interval_ms':0}})
    connection = FakeConnection()
    connection.send_group_forward_msg = AsyncMock(side_effect=ActionError('send_group_forward_msg',retcode=100))
    bot.manager.add(connection)
    try:
        ctx = await failed_reply(bot,connection,ActionError('send_group_msg',retcode=100))
        connection.send_group_msg = AsyncMock(return_value={'message_id':2})
        detail = await bot.deliveries.recover(ctx.turn_id)
        assert detail['status'] == 'delivered'
        assert [part['status'] for part in detail['deliveries']] == ['superseded','sent','sent']
        connection.send_group_msg.assert_awaited_once()
        connection.send_group_forward_msg.assert_awaited_once()
    finally:
        await bot.stop()
