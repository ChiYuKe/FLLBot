import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.models import parse_event
from fllbot.plugin.base import AIToolSpec
from fllbot.plugin.context import Context
from fllbot.storage import Storage
from helpers import FakeConnection, group_message, make_bot


async def test_noncooperative_timeout_is_bounded_and_late_output_cannot_send(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    await bot.permissions.set_plugin_enabled('example',True)
    connection = FakeConnection()
    connection.delete_msg = AsyncMock()
    ctx = Context(bot,connection,parse_event(group_message('hello')))
    release = asyncio.Event()
    expired = []
    async def callback(tool_ctx):
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        await tool_ctx.reply('late answer')
        try:
            await tool_ctx.connection.delete_msg(123)
        except RuntimeError:
            expired.append(True)
        return {'done':True}
    owner = SimpleNamespace(name='example')
    spec = AIToolSpec('send','',{'type':'object'},callback,plugin_name='example',timeout=0.01)
    try:
        result,_ = await asyncio.wait_for(bot.tool_executor.execute(ctx,spec.qualified_name,{},
            {spec.qualified_name:(owner,spec)}),0.5)
        assert json.loads(result)['error'] == 'tool_uncertain'
        assert ctx.data_current and len(bot.tool_executor._callbacks) == 1
        bot.tool_executor.max_callbacks = 1
        other = AsyncMock()
        other_spec = AIToolSpec('other','',{'type':'object'},other,plugin_name='example')
        result,_ = await bot.tool_executor.execute(ctx,other_spec.qualified_name,{},
            {other_spec.qualified_name:(owner,other_spec)})
        assert json.loads(result)['error'] == 'tool_executor_busy'
        other.assert_not_awaited()
        pending = list(bot.tool_executor._callbacks)
        release.set()
        await asyncio.wait_for(asyncio.gather(*pending),1)
        assert not connection.sent and expired == [True]
        connection.delete_msg.assert_not_awaited()
        row = await (await bot.storage.db.execute('SELECT status FROM tool_executions')).fetchone()
        assert row['status'] == 'uncertain'
        assert (await (await bot.storage.db.execute('SELECT count(*) FROM tool_results')).fetchone())[0] == 0
    finally:
        release.set()
        await asyncio.gather(*list(bot.tool_executor._callbacks),return_exceptions=True)
        await bot.stop()


async def test_shutdown_does_not_wait_forever_for_noncooperative_tool(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    await bot.permissions.set_plugin_enabled('example',True)
    release,entered = asyncio.Event(),asyncio.Event()
    async def callback(tool_ctx):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        await tool_ctx.reply('too late')
        return {'done':True}
    owner = SimpleNamespace(name='example')
    spec = AIToolSpec('send','',{'type':'object'},callback,plugin_name='example')
    connection = FakeConnection()
    ctx = Context(bot,connection,parse_event(group_message('hello')))
    async def run():
        async with bot.conversations.turn(ctx):
            await bot.tool_executor.execute(ctx,spec.qualified_name,{}, {spec.qualified_name:(owner,spec)})
    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(),1)
        await asyncio.wait_for(bot.stop(),1)
        with pytest.raises(asyncio.CancelledError):
            await task
        pending = list(bot.tool_executor._callbacks)
        release.set()
        await asyncio.wait_for(asyncio.gather(*pending),1)
        assert not connection.sent
        storage = Storage(bot.storage.path)
        await storage.connect()
        try:
            detail = await storage.turn_detail(ctx.turn_id)
            assert detail['status'] == 'cancelled'
            assert detail['tools'][0]['status'] == 'uncertain'
        finally:
            await storage.close()
    finally:
        release.set()
        await asyncio.gather(task,*list(bot.tool_executor._callbacks),return_exceptions=True)
        await bot.stop()
