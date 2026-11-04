import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


async def test_input_and_model_facts_are_independent_of_delivery(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,builtin=False,llm={'enabled':True,'api_key':'fake'})
    ctx = Context(bot,FakeConnection(),parse_event(group_message('unique input body')))
    ctx.turn_input = 'unique input body'
    monkeypatch.setattr(bot.chat.client,'chat_result',AsyncMock(return_value=SimpleNamespace(
        text='candidate',tool_calls=[],finish_reason='stop',provider='default',model='mock',
        usage={'prompt_tokens':12,'completion_tokens':3})))
    try:
        async with bot.conversations.turn(ctx):
            await bot.chat.complete([{'role':'user','content':'hello'}],scene='group',scene_id=20002,self_id=10001)
        row = await (await bot.storage.db.execute('SELECT content FROM turn_messages WHERE turn_id=?',(ctx.turn_id,))).fetchone()
        assert row['content'] == 'unique input body'
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert detail['model_requests'][0]['prompt_tokens'] == 12
        assert not detail['deliveries'] and not detail['tools']
        assert 'unique input body' not in str(detail)
        await bot.storage.clear_chat('group',20002)
        assert (await (await bot.storage.db.execute('SELECT count(*) FROM turn_messages')).fetchone())[0] == 0
        assert (await bot.storage.turn_detail(ctx.turn_id))['model_requests']
    finally:
        await bot.stop()


async def test_input_failure_rolls_back_turn_and_background_usage_cannot_commit_it(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,builtin=False)
    ctx = Context(bot,FakeConnection(),parse_event(group_message('hello')))
    ctx.turn_input = 'input'
    original = bot.storage.db.execute
    entered, release, writer_entered = asyncio.Event(),asyncio.Event(),asyncio.Event()
    async def execute(sql,*args,**kwargs):
        if sql.startswith('INSERT INTO turn_messages'):
            entered.set()
            await release.wait()
            raise RuntimeError('synthetic input persistence failure')
        return await original(sql,*args,**kwargs)
    monkeypatch.setattr(bot.storage.db,'execute',execute)
    async def turn():
        async with bot.conversations.turn(ctx):
            pytest.fail('failed input transaction must not execute model')
    async def usage():
        writer_entered.set()
        await bot.storage.record_llm_usage(scene='test',scene_id=0,provider='mock',model='mock',
            prompt_tokens=1,completion_tokens=1,latency_ms=1)
    first = asyncio.create_task(turn())
    second = None
    try:
        await asyncio.wait_for(entered.wait(),1)
        second = asyncio.create_task(usage())
        await writer_entered.wait()
        assert not second.done()
        release.set()
        with pytest.raises(RuntimeError):
            await first
        await second
        monkeypatch.setattr(bot.storage.db,'execute',original)
        assert await bot.storage.turn_detail(ctx.turn_id) is None
        assert (await (await bot.storage.db.execute('SELECT count(*) FROM turn_messages')).fetchone())[0] == 0
        assert (await (await bot.storage.db.execute('SELECT count(*) FROM llm_usage')).fetchone())[0] == 1
    finally:
        release.set()
        await asyncio.gather(first,*([second] if second else []),return_exceptions=True)
        await bot.stop()
