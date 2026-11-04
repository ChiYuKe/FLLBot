import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from fllbot.app import create_app
from fllbot.conversation import ServiceClosing
from fllbot.llm.output import send_chat_reply
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from fllbot.storage import Storage
from helpers import FakeConnection, group_message, make_bot


async def test_shutdown_waits_for_inflight_send_receipt_and_refuses_new_debug(tmp_path):
    bot = await make_bot(tmp_path,builtin=False, llm={'enabled':True,'api_key':'fake'},
        overrides={'chat':{'reply_mode':'split','reply_interval_ms':0}})
    entered = asyncio.Event()
    connection = FakeConnection()
    async def waiting(*args,**kwargs):
        entered.set()
        await asyncio.Event().wait()
    connection.send_group_msg = AsyncMock(side_effect=waiting)
    ctx = Context(bot,connection,parse_event(group_message('hello')))
    async def run():
        async with bot.conversations.turn(ctx):
            await send_chat_reply(ctx,'candidate')
    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(),1)
    await asyncio.wait_for(bot.stop(),2)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not ctx.data_current
    assert not bot.conversations._tasks and not bot.conversations._active
    assert bot.capacity.used == 0
    with pytest.raises(ServiceClosing):
        await bot.chat.preview('private',123,'new request')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)),base_url='http://test') as client:
        response = await client.post('/api/llm/chat',headers={'Authorization':'Bearer admin-token'},json={'message':'new'})
        assert response.status_code == 503
    storage = Storage(bot.storage.path)
    await storage.connect()
    try:
        detail = await storage.turn_detail(ctx.turn_id)
        assert detail['status'] == 'delivery_uncertain'
        assert detail['deliveries'][0]['status'] == 'uncertain'
    finally:
        await storage.close()


async def test_handler_can_request_shutdown_without_closing_db_before_its_finalizer(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    ctx = Context(bot,FakeConnection(),parse_event(group_message('hello')))
    async def handler():
        async with bot.conversations.turn(ctx):
            await bot.stop()
    task = asyncio.create_task(handler())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task,2)
    await asyncio.wait_for(bot.stop(),2)
    assert bot.capacity.used == 0 and not bot.conversations._tasks
    storage = Storage(bot.storage.path)
    await storage.connect()
    try:
        detail = await storage.turn_detail(ctx.turn_id)
        assert (detail['status'],detail['reason']) == ('cancelled','service_stopping')
    finally:
        await storage.close()


async def test_shutdown_cancels_pending_debug_and_does_not_revive_diagnostics(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,llm={'enabled':True,'api_key':'fake','context_mode':'custom'})
    entered = asyncio.Event()
    async def waiting(*args,**kwargs):
        entered.set()
        await asyncio.Event().wait()
    bot.chat.complete = waiting
    task = asyncio.create_task(bot.chat.preview('private',123,'hello'))
    await asyncio.wait_for(entered.wait(),1)
    await asyncio.wait_for(bot.stop(),2)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bot.traces.closed and bot.traces.task is None
    await bot.dispatcher.enqueue(parse_event(group_message('late')),FakeConnection())
    assert bot.traces.queue.empty() and not bot.dispatcher._workers
    assert bot.chat.memory._summary_worker is None


async def test_shutdown_records_uncertain_tool_before_closing_storage(tmp_path):
    from types import SimpleNamespace

    from fllbot.plugin.base import AIToolSpec
    bot = await make_bot(tmp_path,builtin=False)
    await bot.permissions.set_plugin_enabled('example',True)
    ctx = Context(bot,FakeConnection(),parse_event(group_message('hello')))
    entered = asyncio.Event()
    async def callback(context):
        entered.set()
        await asyncio.Event().wait()
    owner = SimpleNamespace(name='example')
    spec = AIToolSpec('send','',{'type':'object'},callback,plugin_name='example')
    async def run():
        async with bot.conversations.turn(ctx):
            await bot.tool_executor.execute(ctx,spec.qualified_name,{}, {spec.qualified_name:(owner,spec)})
    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(),1)
    await asyncio.wait_for(bot.stop(),2)
    with pytest.raises(asyncio.CancelledError):
        await task
    storage = Storage(bot.storage.path)
    await storage.connect()
    try:
        detail = await storage.turn_detail(ctx.turn_id)
        assert (detail['status'],detail['reason']) == ('cancelled','service_stopping')
        assert detail['tools'][0]['status'] == 'uncertain'
    finally:
        await storage.close()


async def test_restarting_same_instance_reopens_admission_but_not_old_contexts(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    old = Context(bot,FakeConnection(),parse_event(group_message('old')))
    await bot.stop()
    await bot.start()
    try:
        assert not old.data_current
        fresh = Context(bot,FakeConnection(),parse_event(group_message('fresh',message_id=2)))
        async with bot.conversations.turn(fresh) as accepted:
            assert accepted
        assert bot.capacity.used == 0 and not bot.chat.memory._closing
    finally:
        await bot.stop()
