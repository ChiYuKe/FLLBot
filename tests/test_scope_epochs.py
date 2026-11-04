import asyncio
import time

import aiosqlite
import pytest

from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from fllbot.storage import cleanup
from helpers import FakeConnection, group_message, make_bot


def context(bot,account,group):
    payload = group_message('hello',group_id=group)
    payload['self_id'] = account
    return Context(bot,FakeConnection(self_id=account),parse_event(payload))


@pytest.mark.parametrize('operation', ['clear', 'retention'])
async def test_cancel_at_committed_clear_cannot_leave_live_old_epoch(tmp_path, monkeypatch, operation):
    bot = await make_bot(tmp_path, builtin=False)
    committed, release = asyncio.Event(), asyncio.Event()
    original = aiosqlite.Connection.commit
    target = context(bot, 11, 123)
    try:
        await bot.storage.add_chat_message('group', 123, 'user', 'old', self_id=11)

        async def paused_commit(connection):
            await original(connection)
            committed.set()
            await release.wait()

        monkeypatch.setattr(aiosqlite.Connection, 'commit', paused_commit)
        if operation == 'clear':
            pending = bot.storage.clear_chat('group', 123, self_id=11)
        else:
            pending = cleanup.execute(bot.storage, {'self_id': 11, 'scene': 'group', 'scene_id': 123,
                                                   'cutoffs': {'conversations': time.time() + 1}})
        task = asyncio.create_task(pending)
        await asyncio.wait_for(committed.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        # A body write queued behind the clear must not enter before the committed epoch is published.
        write = asyncio.create_task(bot.storage.add_chat_message(
            'group', 123, 'assistant', 'stale', self_id=11, generation=target.data_epoch,
            account_epoch=target.received.account_epoch, scope_epoch=target.received.scope_generation))
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await write
        assert not target.data_current
        assert bot.storage.scope_generation(11, 'group', 123) == 1
        assert not await bot.storage.recent_chat('group', 123, self_id=11)
        row = await (await bot.storage.db.execute(
            "SELECT version FROM conversation_epochs WHERE self_id=11 AND scene='group' AND scene_id=123")).fetchone()
        assert row[0] == 1
    finally:
        release.set()
        monkeypatch.setattr(aiosqlite.Connection, 'commit', original)
        await bot.stop()


@pytest.mark.parametrize('operation', ['clear', 'retention'])
async def test_failed_clear_commit_keeps_data_and_epoch_then_allows_retry(tmp_path, monkeypatch, operation):
    bot = await make_bot(tmp_path, builtin=False)
    original = aiosqlite.Connection.commit
    target = context(bot, 11, 123)
    try:
        await bot.storage.add_chat_message('group', 123, 'user', 'keep', self_id=11)

        async def failed_commit(connection):
            raise OSError('simulated commit failure')

        monkeypatch.setattr(aiosqlite.Connection, 'commit', failed_commit)
        with pytest.raises(OSError, match='commit failure'):
            if operation == 'clear':
                await bot.storage.clear_chat('group', 123, self_id=11)
            else:
                await cleanup.execute(bot.storage, {'self_id': 11, 'scene': 'group', 'scene_id': 123,
                                                   'cutoffs': {'conversations': time.time() + 1}})
        assert target.data_current
        assert bot.storage.scope_generation(11, 'group', 123) == 0
        assert [row.content for row in await bot.storage.recent_chat('group', 123, self_id=11)] == ['keep']
        assert (await (await bot.storage.db.execute('SELECT count(*) FROM conversation_epochs')).fetchone())[0] == 0
        monkeypatch.setattr(aiosqlite.Connection, 'commit', original)
        await bot.storage.clear_chat('group', 123, self_id=11)
        assert not target.data_current
    finally:
        monkeypatch.setattr(aiosqlite.Connection, 'commit', original)
        await bot.stop()


async def test_precise_clear_invalidates_only_one_conversation(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    try:
        target,other_account,other_group = context(bot,11,123),context(bot,22,123),context(bot,11,456)
        await bot.storage.clear_chat('group',123,self_id=11)
        assert not target.data_current
        assert other_account.data_current and other_group.data_current
        fresh = context(bot,11,123)
        assert fresh.data_current and fresh.received.scope_generation == 1
        async with bot.conversations.turn(target) as accepted:
            assert not accepted
        async with bot.conversations.turn(other_account) as accepted:
            assert accepted
    finally:
        await bot.stop()


async def test_waiting_writes_recheck_conversation_epoch_without_blocking_other_account(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    contexts = [context(bot,account,123) for account in (11,22)]
    entered = [asyncio.Event(),asyncio.Event()]
    async def write(index,ctx):
        entered[index].set()
        await bot.storage.memory.append(ctx.self_id,123,'user','source '+str(ctx.self_id),created_at=100,
            generation=ctx.data_epoch,account_epoch=ctx.received.account_epoch,scope_epoch=ctx.received.scope_generation)
        await bot.storage.add_chat_message('group',123,'user','source '+str(ctx.self_id),self_id=ctx.self_id,
            generation=ctx.data_epoch,account_epoch=ctx.received.account_epoch,scope_epoch=ctx.received.scope_generation)
    try:
        async with bot.storage.data_lock:
            tasks = [asyncio.create_task(write(index,ctx)) for index,ctx in enumerate(contexts)]
            await asyncio.gather(*(event.wait() for event in entered))
            await bot.storage.clear_chat('group',123,self_id=11)
        await asyncio.gather(*tasks)
        assert not await bot.storage.memory.recent(11,123,999)
        assert not await bot.storage.recent_chat('group',123,self_id=11)
        assert (await bot.storage.memory.recent(22,123,999))[0]['content'] == 'source 22'
        assert (await bot.storage.recent_chat('group',123,self_id=22))[0].content == 'source 22'
    finally:
        await bot.stop()


async def test_precise_clear_drops_only_matching_queued_message(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,builtin=False,overrides={'plugin':{'max_concurrency':1}})
    entered,release = asyncio.Event(),asyncio.Event()
    seen=[]
    async def dispatch(event,*args):
        seen.append(event.message_id)
        if event.message_id == 1:
            entered.set()
            await release.wait()
    monkeypatch.setattr(bot.dispatcher,'_dispatch_guarded',dispatch)
    try:
        for account,message in [(33,1),(11,2),(22,3)]:
            payload = group_message('hello',group_id=123,message_id=message)
            payload['self_id'] = account
            await bot.dispatcher.enqueue(parse_event(payload),FakeConnection(self_id=account))
            if message==1:
                await asyncio.wait_for(entered.wait(),1)
        await bot.storage.clear_chat('group',123,self_id=11)
        release.set()
        await asyncio.wait_for(bot.dispatcher._queue.join(),1)
        assert seen == [1,3]
        assert bot.capacity.used == 0
    finally:
        release.set()
        await bot.stop()


async def test_scoped_epoch_survives_reconnect_and_new_storage_instance(tmp_path):
    from fllbot.storage import Storage
    bot = await make_bot(tmp_path,builtin=False)
    try:
        await bot.storage.clear_chat('group',123,self_id=11)
        assert bot.storage.scope_generation(11,'group',123) == 1
        path = bot.storage.path
        await bot.storage.close()
        other = Storage(path)
        await other.connect()
        try:
            assert other.scope_generation(11,'group',123) == 1
            assert other.scope_generation(22,'group',123) == 0
        finally:
            await other.close()
        await bot.storage.connect()
        assert context(bot,11,123).received.scope_generation == 1
    finally:
        await bot.stop()
