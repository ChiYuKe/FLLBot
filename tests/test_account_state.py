import asyncio

import httpx
import pytest

from fllbot.app import create_app
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot

AUTH = {'Authorization':'Bearer admin-token'}


async def test_disable_cancels_turn_and_old_context_stays_invalid_after_enable(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    await bot.bots.update(10001, nickname='example')
    ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
    entered = asyncio.Event()
    async def run():
        async with bot.conversations.turn(ctx):
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(),1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)),base_url='http://test') as client:
            url = '/api/console/bots/10001/state'
            assert (await client.put(url,json={'enabled':False})).status_code == 401
            assert (await client.put(url,headers=AUTH,json={'enabled':False})).status_code == 200
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not ctx.data_current
            detail = await bot.storage.turn_detail(ctx.turn_id)
            assert (detail['status'],detail['reason']) == ('cancelled','account_disabled')
            assert (await client.put(url,headers=AUTH,json={'enabled':True})).status_code == 200
            assert not ctx.data_current
            fresh = Context(bot,FakeConnection(),parse_event(group_message('new',message_id=2)))
            assert fresh.data_current
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        await bot.stop()


async def test_old_queued_event_is_not_revived_by_reenable(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,builtin=False,overrides={'plugin':{'max_concurrency':1}})
    entered, release = asyncio.Event(),asyncio.Event()
    seen = []
    async def dispatch(event,*args,**kwargs):
        seen.append(event.message_id)
        entered.set()
        await release.wait()
    monkeypatch.setattr(bot.dispatcher,'_dispatch_guarded',dispatch)
    try:
        bot.dispatcher.emit(parse_event(group_message('first',message_id=1)),FakeConnection())
        await asyncio.wait_for(entered.wait(),1)
        bot.dispatcher.emit(parse_event(group_message('queued',message_id=2)),FakeConnection())
        await bot.bots.update(10001,enabled=False)
        await bot.bots.update(10001,enabled=True)
        release.set()
        await asyncio.wait_for(bot.dispatcher._queue.join(),1)
        assert seen == [1]
        assert bot.dispatcher.stats['cancelled_account'] == 1
    finally:
        release.set()
        await bot.stop()


async def test_disabled_state_survives_restart(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    await bot.bots.update(10001,enabled=False)
    await bot.stop()
    restored = await make_bot(tmp_path,builtin=False)
    try:
        assert not restored.bots.enabled(10001)
        assert restored.bots.epoch(10001) == 1
    finally:
        await restored.stop()
