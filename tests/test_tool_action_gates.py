import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from fllbot.onebot.models import parse_event
from fllbot.plugin.base import AIToolSpec
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


@pytest.mark.parametrize('change',['plugin','group','whitelist','permission','clear','account','disconnect','schema','callback','tool_permission'])
async def test_tool_rechecks_before_deferred_protocol_call(tmp_path,change):
    bot = await make_bot(tmp_path,builtin=False)
    entered,release = asyncio.Event(),asyncio.Event()
    ctx = Context(bot,FakeConnection(),parse_event(group_message('hello')))
    ctx.event.sender.role = 'admin'
    owner = SimpleNamespace(name='example')
    saved = []
    async def callback(tool_ctx):
        await tool_ctx.reply('already sent')
        # Obtaining a coroutine before revocation must not bypass the await-time gate.
        deferred = tool_ctx.connection.send_group_msg(ctx.group_id,'must be blocked')
        saved.append(tool_ctx)
        entered.set()
        await release.wait()
        await deferred
        return {'done':True}
    spec = AIToolSpec('send','',{'type':'object'},callback,permission='group_admin',plugin_name='example')
    registry = {spec.qualified_name:(owner,spec)}
    task = asyncio.create_task(bot.tool_executor.execute(ctx,spec.qualified_name,{},registry,
        refresh=lambda _: ([],registry)))
    try:
        await asyncio.wait_for(entered.wait(),1)
        if change == 'plugin':
            await bot.permissions.set_plugin_enabled('example',False)
        elif change == 'group':
            await bot.permissions.set_group_enabled(ctx.group_id,False)
        elif change == 'whitelist':
            registry.clear()
        elif change == 'permission':
            ctx.event.sender.role = 'member'
        elif change == 'clear':
            await bot.storage.clear_chat('group',ctx.group_id,self_id=ctx.self_id)
        elif change == 'account':
            await bot.bots.update(ctx.self_id,enabled=False)
        elif change in ('schema','callback','tool_permission'):
            live = copy.copy(spec)
            if change == 'schema':
                live.parameters = {'type':'object','required':['new']}
            elif change == 'callback':
                async def replaced(ctx):
                    return {}
                live.callback = replaced
            else:
                live.permission = 'superuser'
            registry[spec.qualified_name] = (owner,live)
        else:
            ctx.connection._closed = True
        assert not saved[0].data_current
        release.set()
        output,_ = await asyncio.wait_for(task,1)
        assert json.loads(output)['error'] == 'tool_uncertain'
        assert ctx.connection.texts() == ['already sent']
        row = await (await bot.storage.db.execute('SELECT status FROM tool_executions')).fetchone()
        assert row['status'] == 'uncertain'
    finally:
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        await bot.stop()
