import asyncio

from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


async def test_persistent_event_dedup_includes_account_and_scene(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,builtin=False)
    seen = []
    async def dispatch(event,*args):
        seen.append((event.self_id,event.scene_id))
    monkeypatch.setattr(bot.dispatcher,'_dispatch_guarded',dispatch)
    try:
        for account,group in [(10001,1),(10001,2),(20001,1)]:
            event = parse_event(group_message('private source',group_id=group,message_id=7))
            event.self_id = account
            await bot.dispatcher.enqueue(event,FakeConnection())
        await bot.dispatcher._queue.join()
        await bot.dispatcher.drain()
        await bot.storage.close()
        await bot.storage.connect()
        again = parse_event(group_message('private source',group_id=1,message_id=7))
        await bot.dispatcher.enqueue(again,FakeConnection())
        assert len(seen) == 3
        facts = await bot.storage.list_message_events()
        assert len(facts['items']) == 3
        assert all(item['status']=='completed' for item in facts['items'])
        assert 'private source' not in str(facts)
    finally:
        await bot.stop()


async def test_overflow_fact_is_preserved_and_cancel_does_not_kill_worker(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,builtin=False,overrides={'plugin':{'max_concurrency':1,'max_pending_events':1}})
    entered,release = asyncio.Event(),asyncio.Event()
    seen = []
    async def dispatch(event,*args):
        seen.append(event.message_id)
        if event.message_id==1:
            entered.set()
            await release.wait()
            raise asyncio.CancelledError()
    monkeypatch.setattr(bot.dispatcher,'_dispatch_guarded',dispatch)
    try:
        await bot.dispatcher.enqueue(parse_event(group_message('one',message_id=1)),FakeConnection())
        await asyncio.wait_for(entered.wait(),1)
        await bot.dispatcher.enqueue(parse_event(group_message('two',message_id=2)),FakeConnection())
        await bot.dispatcher.enqueue(parse_event(group_message('three',message_id=3)),FakeConnection())
        release.set()
        await asyncio.wait_for(bot.dispatcher._queue.join(),1)
        facts = (await bot.storage.list_message_events())['items']
        assert [(row['message_id'],row['status']) for row in reversed(facts)] == [(1,'cancelled'),(2,'completed'),(3,'dropped')]
        assert seen == [1,2]
        assert all(not task.done() for task in bot.dispatcher._workers)
    finally:
        release.set()
        await bot.stop()


async def test_ephemeral_envelope_keeps_turn_link_without_claiming_stable_dedup(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,builtin=False)
    turns = []
    async def dispatch(event,connection,*args):
        ctx = Context(bot,connection,event)
        turns.append(ctx.turn_id)
    monkeypatch.setattr(bot.dispatcher,'_dispatch_guarded',dispatch)
    try:
        for _ in range(2):
            await bot.dispatcher.enqueue(parse_event(group_message('hello',message_id=0)),FakeConnection())
        await bot.dispatcher._queue.join()
        facts = (await bot.storage.list_message_events())['items']
        assert {row['turn_id'] for row in facts} == set(turns)
        assert len(set(turns)) == 2 and not any(row['stable_id'] for row in facts)
    finally:
        await bot.stop()


async def test_missing_message_id_uses_only_bounded_in_memory_dedup(tmp_path):
    from fllbot.app import _dedupe_key
    bot = await make_bot(tmp_path,builtin=False)
    try:
        event = parse_event(group_message('hello',message_id=0))
        key = _dedupe_key(event)
        assert key.startswith('ephemeral_message:')
        assert not bot._dedupe.seen(key)
        assert bot._dedupe.seen(key)
        other = parse_event(group_message('hello',group_id=999,message_id=0))
        assert _dedupe_key(other) != key
    finally:
        await bot.stop()


async def test_queued_event_cannot_be_changed_through_the_callers_reference(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,builtin=False,overrides={'plugin':{'max_concurrency':1}})
    entered,release = asyncio.Event(),asyncio.Event()
    seen = []
    async def dispatch(event,connection,*args):
        if event.message_id == 1:
            entered.set()
            await release.wait()
        ctx = Context(bot,connection,event)
        ctx.turn_input = ctx.text
        async with bot.conversations.turn(ctx) as accepted:
            assert accepted
            seen.append((ctx.self_id,ctx.group_id,ctx.message_id,ctx.text))
    monkeypatch.setattr(bot.dispatcher,'_dispatch_guarded',dispatch)
    try:
        await bot.dispatcher.enqueue(parse_event(group_message('first',message_id=1)),FakeConnection())
        await asyncio.wait_for(entered.wait(),1)
        queued = parse_event(group_message('original',message_id=2))
        await bot.dispatcher.enqueue(queued,FakeConnection())
        queued.self_id,queued.group_id,queued.message_id = 22,999,99
        queued.message[0].data['text'] = 'modified'
        release.set()
        await asyncio.wait_for(bot.dispatcher._queue.join(),1)
        assert seen == [(10001,20002,1,'first'),(10001,20002,2,'original')]
        rows = await (await bot.storage.db.execute('SELECT content FROM turn_messages ORDER BY id')).fetchall()
        assert [row['content'] for row in rows] == ['first','original']
    finally:
        release.set()
        await bot.stop()
