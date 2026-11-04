import asyncio
from pathlib import Path

import pytest

from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


async def test_duplicate_turn_is_claimed_once_and_retains_reason(tmp_path: Path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        for index in range(2):
            ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
            async with bot.conversations.turn(ctx) as accepted:
                assert accepted is (index == 0)
                if accepted:
                    ctx.turn_reason = 'model_failed'
        rows = await (await bot.storage.db.execute('SELECT status,reason FROM turns')).fetchall()
        assert [(row['status'],row['reason']) for row in rows] == [('failed','model_failed')]
    finally:
        await bot.stop()


async def test_turn_timeout_persists_expired(tmp_path: Path):
    bot = await make_bot(tmp_path, builtin=False, overrides={'plugin':{'event_deadline_seconds':0.01}})
    try:
        ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        with pytest.raises(TimeoutError):
            async with bot.conversations.turn(ctx) as accepted:
                assert accepted
                await asyncio.Event().wait()
        row = await (await bot.storage.db.execute('SELECT status,reason FROM turns')).fetchone()
        assert (row['status'],row['reason']) == ('expired','deadline_exceeded')
    finally:
        await bot.stop()


async def test_restart_does_not_reopen_interrupted_turn(tmp_path: Path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        assert await bot.storage.create_turn(ctx, 9999999999, 'config', 'trace')
        await bot.storage.set_turn_status(ctx.turn_id, 'delivering')
        await bot.storage.close()
        await bot.storage.connect()
        async with bot.conversations.turn(ctx) as accepted:
            assert not accepted
        row = await (await bot.storage.db.execute('SELECT status,reason FROM turns')).fetchone()
        assert (row['status'],row['reason']) == ('delivery_uncertain','process_restart')
    finally:
        await bot.stop()


async def test_receive_snapshot_survives_context_and_turn_creation(tmp_path):
    from fllbot.conversation import ReceiveSnapshot, receive_scope
    bot = await make_bot(tmp_path, builtin=False)
    try:
        captured = ReceiveSnapshot.capture(bot.storage.conversation_generation, bot.config.plugin)
        with receive_scope(captured):
            ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        assert ctx.received.received_at == captured.received_at
        assert ctx.received.deadline == captured.deadline
        assert ctx.received.monotonic_deadline == captured.monotonic_deadline
        assert ctx.received.data_epoch == captured.data_epoch
        async with bot.conversations.turn(ctx) as accepted:
            assert accepted
        row = await (await bot.storage.db.execute('SELECT deadline,data_epoch FROM turns')).fetchone()
        assert row['deadline'] == captured.deadline
        assert row['data_epoch'] == captured.data_epoch
        await bot.storage.clear_chat('group', 20002)
        with receive_scope(captured):
            stale = Context(bot, FakeConnection(), parse_event(group_message('two', message_id=2)))
        assert not stale.data_current
        async with bot.conversations.turn(stale) as accepted:
            assert not accepted
    finally:
        await bot.stop()
