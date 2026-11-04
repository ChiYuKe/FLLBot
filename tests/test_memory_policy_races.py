import asyncio
import time

import pytest

from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


async def configured_bot(tmp_path):
    bot = await make_bot(tmp_path,llm={'enabled':True,'api_key':'fake'})
    await bot.permissions.save_group(20002,enabled=True,config={'policy':{'auto_group':True,'cooldown_seconds':0}})
    return bot


@pytest.mark.parametrize('change',['privacy','memory','group','plugin','auto_group','llm'])
async def test_summary_waiting_for_commit_rechecks_latest_settings(tmp_path,change):
    bot = await configured_bot(tmp_path)
    source = await bot.storage.memory.append(10001,20002,'user','allowed original',created_at=time.time())
    rows = await bot.storage.memory.recent(10001,20002,source+1)
    ctx = Context(bot,FakeConnection(),parse_event(group_message('hello')))
    privacy = bot.chat.memory.privacy()
    entered = asyncio.Event()
    async def commit():
        entered.set()
        await bot.chat.memory.commit_summary(ctx,rows,privacy,[{'topic':'old summary'}])
    task = None
    try:
        async with bot.storage.settings_lock:
            task = asyncio.create_task(commit())
            await entered.wait()
            if change=='privacy':
                await bot.update_config({'chat':{'send_nickname':False}})
            elif change=='memory':
                await bot.update_config({'chat':{'group_memory':False}})
            elif change=='group':
                await bot.permissions.set_group_enabled(20002,False)
            elif change=='plugin':
                await bot.permissions.set_plugin_enabled('agent',False)
            elif change=='auto_group':
                await bot.permissions.save_group(20002,enabled=True,config={'policy':{'auto_group':False}})
            else:
                await bot.update_config({'llm':{'enable_group':False}})
        await task
        assert not await bot.storage.memory.summaries(10001,20002,privacy,0,source+1)
        assert await bot.storage.memory.recent(10001,20002,source+1)
    finally:
        if task:
            await asyncio.gather(task,return_exceptions=True)
        await bot.stop()


async def test_messages_received_while_capture_is_off_do_not_backfill_after_reenable(tmp_path,monkeypatch):
    bot = await configured_bot(tmp_path)
    await bot.update_config({'chat':{'group_memory':False}})
    entered,release = asyncio.Event(),asyncio.Event()
    async def dispatch(event,connection,*args):
        if event.message_id==1:
            entered.set()
            await release.wait()
        else:
            await bot.chat.memory.observe(Context(bot,connection,event))
    monkeypatch.setattr(bot.dispatcher,'_dispatch_guarded',dispatch)
    # One worker makes the receive-time/commit-time boundary deterministic.
    bot.dispatcher._worker_count = 1
    try:
        await bot.dispatcher.enqueue(parse_event(group_message('blocking',message_id=1)),FakeConnection())
        await asyncio.wait_for(entered.wait(),1)
        await bot.dispatcher.enqueue(parse_event(group_message('received while off',message_id=2)),FakeConnection())
        await bot.update_config({'chat':{'group_memory':True}})
        release.set()
        await asyncio.wait_for(bot.dispatcher._queue.join(),1)
        await bot.dispatcher.enqueue(parse_event(group_message('received while on',message_id=3)),FakeConnection())
        await asyncio.wait_for(bot.dispatcher._queue.join(),1)
        rows = await bot.storage.memory.recent(10001,20002,9999)
        assert [row['source_id'] for row in rows] == [3]
    finally:
        release.set()
        await bot.stop()


async def test_summary_commit_uses_live_privacy_even_inside_frozen_execution_scope(tmp_path):
    bot = await configured_bot(tmp_path)
    try:
        source = await bot.storage.memory.append(10001,20002,'user','source',created_at=time.time())
        rows = await bot.storage.memory.recent(10001,20002,source+1)
        ctx = Context(bot,FakeConnection(),parse_event(group_message('hello')))
        with bot.chat.execution_scope('group',20002,10001):
            privacy = bot.chat.memory.privacy()
            await bot.update_config({'chat':{'send_nickname':False}})
            assert bot.chat.memory.privacy() == privacy
            await bot.chat.memory.commit_summary(ctx,rows,privacy,[{'topic':'old policy'}])
        assert not await bot.storage.memory.summaries(10001,20002,privacy,0,source+1)
    finally:
        await bot.stop()
