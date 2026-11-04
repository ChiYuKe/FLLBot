import asyncio

import httpx
import pytest

from fllbot.app import create_app
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot

AUTH = {'Authorization':'Bearer admin-token'}


async def test_delivery_metrics_count_uncertain_facts_and_respect_conversation_filter(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    try:
        for group,state in [(123,'uncertain'),(456,'sent')]:
            ctx = Context(bot,FakeConnection(),parse_event(group_message('secret',group_id=group)))
            async with bot.conversations.turn(ctx):
                await bot.storage.prepare_delivery(str(group),ctx.self_id,'group',group,
                    'secret candidate',['secret part'],ctx.data_epoch,ctx.turn_id)
                await bot.storage.update_delivery(str(group),0,'sending')
                await bot.storage.update_delivery(str(group),0,state)
                if state == 'uncertain':
                    await bot.storage.claim_tool('execution',ctx.self_id,'group',group,ctx.message_id,
                        'action','side_effecting',ctx.data_epoch,ctx.turn_id,'call','hash')
                    await bot.storage.finish_tool('execution','uncertain')
        metrics = await bot.storage.delivery_metrics()
        assert metrics['parts'] == {'sent':1,'uncertain':1}
        assert metrics['uncertain_tools'] == 1 and metrics['confirmed_delivery_rate'] == 0.5
        scoped = await bot.storage.delivery_metrics(scene='group',scene_id=456)
        assert scoped['parts'] == {'sent':1} and scoped['uncertain_tools'] == 0
        assert scoped['confirmed_delivery_rate'] == 1.0
        assert 'secret' not in str(metrics)
    finally:
        await bot.stop()


async def test_turn_query_is_authenticated_scoped_and_metadata_only(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        ctx = Context(bot, FakeConnection(), parse_event(group_message('private text')))
        async with bot.conversations.turn(ctx):
            await bot.storage.prepare_delivery('delivery',ctx.self_id,'group',ctx.group_id,
                'private candidate',['private part'],ctx.data_epoch,ctx.turn_id)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            assert (await client.get('/api/console/turns')).status_code == 401
            assert (await client.get('/api/console/turns/'+ctx.turn_id)).status_code == 401
            assert (await client.post('/api/console/turns/'+ctx.turn_id+'/cancel')).status_code == 401
            assert (await client.post('/api/console/turns/'+ctx.turn_id+'/recover-delivery')).status_code == 401
            listing = (await client.get('/api/console/turns?self_id=10001',headers=AUTH)).json()
            assert len(listing['items']) == 1
            assert not (await client.get('/api/console/turns?self_id=22',headers=AUTH)).json()['items']
            detail = (await client.get('/api/console/turns/'+ctx.turn_id,headers=AUTH)).json()
            assert detail['deliveries'][0]['status'] == 'pending'
            assert not detail['can_retry'] and not detail['can_cancel']
            assert 'private' not in str(detail)
            assert (await client.post('/api/console/turns/'+ctx.turn_id+'/cancel',headers=AUTH)).status_code == 409
    finally:
        await bot.stop()


async def test_cancel_running_turn_preserves_terminal_fact(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    entered = asyncio.Event()
    ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
    async def run():
        async with bot.conversations.turn(ctx) as accepted:
            assert accepted
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            response = await client.post('/api/console/turns/'+ctx.turn_id+'/cancel',headers=AUTH)
            assert response.status_code == 200
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not ctx.data_current
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert (detail['status'],detail['reason']) == ('cancelled','admin_cancelled')
        assert not bot.conversations.can_cancel(ctx.turn_id)
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        await bot.stop()
