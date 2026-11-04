import asyncio

import pytest

from fllbot.event_queue import ConversationQueue
from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot


def item(group, number):
    payload = group_message(str(number))
    payload['group_id'] = group
    return (parse_event(payload), FakeConnection(), 'message', None, 0)


async def test_busy_conversation_does_not_hold_second_worker():
    queue = ConversationQueue(10, 3)
    queue.put_nowait(item(1, 1))
    queue.put_nowait(item(1, 2))
    queue.put_nowait(item(2, 3))
    first = await queue.get()
    assert first[0].group_id == 1
    async def second_worker():
        second = await queue.get()
        assert second[0].group_id == 2
        queue.task_done()
    await asyncio.wait_for(asyncio.create_task(second_worker()), 1)
    queue.task_done()
    assert (await queue.get())[0].group_id == 1
    queue.task_done()
    await queue.join()
    assert not queue._pending and not queue._active


async def test_both_queue_limits_and_discard():
    queue = ConversationQueue(3, 2)
    queue.put_nowait(item(1, 1))
    queue.put_nowait(item(1, 2))
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(item(1, 3))
    queue.put_nowait(item(2, 4))
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(item(3, 5))
    assert len(queue.discard_pending()) == 3
    await queue.join()
    assert not queue._pending


async def test_expired_queue_never_dispatches(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, builtin=False)
    seen = []
    async def dispatch(*args, **kwargs):
        seen.append(args)
    monkeypatch.setattr(bot.dispatcher, '_dispatch_guarded', dispatch)
    try:
        bot.dispatcher._queue.put_nowait(item(1, 1))
        bot.dispatcher.start()
        await asyncio.wait_for(bot.dispatcher._queue.join(), 1)
        assert not seen
        assert bot.dispatcher.stats['expired_queued'] == 1
    finally:
        await bot.stop()


async def test_clear_invalidates_events_already_in_queue(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, builtin=False, overrides={'plugin':{'max_concurrency':1}})
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def dispatch(event, *args, **kwargs):
        seen.append(event.message_id)
        entered.set()
        await release.wait()
    monkeypatch.setattr(bot.dispatcher, '_dispatch_guarded', dispatch)
    try:
        bot.dispatcher.emit(parse_event(group_message('one', message_id=1)), FakeConnection())
        await asyncio.wait_for(entered.wait(), 1)
        bot.dispatcher.emit(parse_event(group_message('two', message_id=2)), FakeConnection())
        await bot.storage.clear_chat('group', 20002)
        release.set()
        await asyncio.wait_for(bot.dispatcher._queue.join(), 1)
        assert seen == [1]
        assert bot.dispatcher.stats['cancelled_queued'] == 1
    finally:
        release.set()
        await bot.stop()
