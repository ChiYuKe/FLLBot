import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from fllbot.llm.output import send_chat_reply
from fllbot.onebot.connection import ActionError
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from fllbot.storage import cleanup
from helpers import FakeConnection, group_message, make_bot


@pytest.mark.parametrize('retention',[False,True])
@pytest.mark.parametrize('timeout',[False,True])
async def test_clear_during_send_preserves_late_receipt_without_resurrecting_body(tmp_path,retention,timeout):
    bot = await make_bot(tmp_path,builtin=False,overrides={'chat':{
        'reply_mode':'split','reply_chunk_size':50,'reply_interval_ms':0}})
    entered, release = asyncio.Event(),asyncio.Event()
    connection = FakeConnection()
    async def send(*args,**kwargs):
        entered.set()
        await release.wait()
        if timeout:
            raise ActionError('send_group_msg',message='timeout')
        return {'message_id':333}
    connection.send_group_msg = AsyncMock(side_effect=send)
    ctx = Context(bot,connection,parse_event(group_message('hello')))
    async def run():
        async with bot.conversations.turn(ctx):
            await send_chat_reply(ctx,'private candidate '*8)
    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(),1)
        if retention:
            spec={'scene':'group','scene_id':20002,'self_id':10001,'cutoffs':{'conversations':time.time()+1}}
            preview = await cleanup.preview(bot.storage,spec)
            await cleanup.execute(bot.storage,spec,expected=preview['signature'])
            assert (await cleanup.preview(bot.storage,spec))['counts']['reply_deliveries'] == 0
        else:
            await bot.storage.clear_chat('group',20002,self_id=10001)
        release.set()
        if timeout:
            with pytest.raises(ActionError):
                await task
        else:
            await task
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert detail['deliveries'][0]['status'] == ('uncertain' if timeout else 'sent')
        assert detail['deliveries'][0]['message_id'] == ('' if timeout else '333')
        assert all(part['status']=='cancelled' for part in detail['deliveries'][1:])
        assert detail['status'] == ('delivery_uncertain' if timeout else 'cancelled')
        assert not await bot.storage.recent_chat('group',20002,self_id=10001)
        assert not await bot.storage.recovery_plan(ctx.turn_id)
        plan = await (await bot.storage.db.execute('SELECT * FROM reply_deliveries')).fetchone()
        assert (plan['candidate'],plan['metadata'],plan['bodies_cleared']) == ('','{}',1)
        parts = await (await bot.storage.db.execute('SELECT content FROM delivery_parts')).fetchall()
        assert all(part['content']=='' for part in parts)
        connection.send_group_msg.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        await bot.stop()
