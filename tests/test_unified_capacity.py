import asyncio

import pytest

from fllbot.conversation import CapacityExceeded
from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot


async def test_qq_admission_limits_direct_debug_and_releases_after_completion(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,builtin=False,overrides={'plugin':{'max_concurrency':1,'max_pending_events':1}})
    entered, release = asyncio.Event(),asyncio.Event()
    async def dispatch(*args):
        entered.set()
        await release.wait()
    monkeypatch.setattr(bot.dispatcher,'_dispatch_guarded',dispatch)
    try:
        await bot.dispatcher.enqueue(parse_event(group_message('active',message_id=1)),FakeConnection())
        await asyncio.wait_for(entered.wait(),1)
        await bot.dispatcher.enqueue(parse_event(group_message('queued',message_id=2)),FakeConnection())
        assert bot.capacity.used == 2
        ctx = bot.debug_context('private',123)
        with pytest.raises(CapacityExceeded):
            async with bot.conversations.turn(ctx):
                pytest.fail('must not execute')
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert detail['reason'] == 'global_queue_full'
        release.set()
        await asyncio.wait_for(bot.dispatcher._queue.join(),1)
        assert bot.capacity.used == 0 and not bot.capacity.counts
        async with bot.conversations.turn(bot.debug_context('private',123)) as accepted:
            assert accepted and bot.capacity.used == 1
        assert bot.capacity.used == 0
    finally:
        release.set()
        await bot.stop()


async def test_direct_turn_queue_timeout_releases_capacity(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,overrides={'plugin':{'queue_timeout_seconds':0.01,'event_deadline_seconds':1}})
    ctx = bot.debug_context('private',123)
    try:
        async with bot.chat.conversation_lock(0,'private',123):
            async def waiting():
                with pytest.raises(TimeoutError):
                    async with bot.conversations.turn(ctx):
                        pytest.fail('expired queue must not execute')
            await asyncio.create_task(waiting())
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert (detail['status'],detail['reason']) == ('expired','queue_expired')
        assert bot.capacity.used == 0 and not bot.capacity.counts
    finally:
        await bot.stop()


async def test_shutdown_releases_active_and_pending_admission(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,builtin=False,overrides={'plugin':{'max_concurrency':1,'max_pending_events':1}})
    entered = asyncio.Event()
    async def waiting(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(bot.dispatcher,'_dispatch_guarded',waiting)
    try:
        await bot.dispatcher.enqueue(parse_event(group_message('active',message_id=1)),FakeConnection())
        await asyncio.wait_for(entered.wait(),1)
        await bot.dispatcher.enqueue(parse_event(group_message('queued',message_id=2)),FakeConnection())
        assert bot.capacity.used == 2
        await bot.dispatcher.drain(timeout=0.01)
        assert bot.capacity.used == 0 and not bot.capacity.counts
        facts = (await bot.storage.list_message_events())['items']
        assert all(row['status']=='cancelled' for row in facts)
    finally:
        await bot.stop()
