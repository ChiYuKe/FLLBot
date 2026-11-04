import asyncio
from unittest.mock import AsyncMock

import pytest

from fllbot.llm.chat import ChatReply
from fllbot.llm.client import LLMError
from helpers import make_bot


async def test_account_preview_reads_only_selected_history_and_persists_metadata(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,llm={'enabled':True,'api_key':'fake','context_mode':'custom'})
    try:
        await bot.bots.update(11,nickname='one')
        await bot.storage.add_chat_message('group',123,'user','account one',self_id=11)
        await bot.storage.add_chat_message('group',123,'user','account two',self_id=22)
        bot.chat.complete = AsyncMock(return_value=ChatReply('preview answer','','model'))
        reply = await bot.chat.preview('group',123,'hello',self_id=11)
        messages = bot.chat.complete.call_args.args[0]
        assert any('account one' in item['content'] for item in messages)
        assert not any('account two' in item['content'] for item in messages)
        assert [row.content for row in await bot.storage.recent_chat('group',123,self_id=11)] == ['account one']
        detail = await bot.storage.turn_detail(reply['turn_id'])
        assert detail['entrypoint'] == 'admin_debug'
        assert (detail['status'],detail['reason']) == ('silent','admin_debug_completed')
        assert not detail['deliveries'] and not detail['tools']
        with pytest.raises(ValueError):
            await bot.chat.preview('group',123,'hello',self_id=11,save=True)
    finally:
        await bot.stop()


async def test_debug_model_failure_is_visible_in_turns(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,llm={'enabled':True,'api_key':'fake','context_mode':'custom'})
    try:
        bot.chat.complete = AsyncMock(side_effect=LLMError('synthetic failure'))
        with pytest.raises(LLMError):
            await bot.chat.preview('private',123,'hello')
        items = (await bot.storage.list_turns())['items']
        assert len(items) == 1
        assert items[0]['entrypoint'] == 'admin_debug' and items[0]['status'] == 'failed'
    finally:
        await bot.stop()


async def test_debug_execution_can_be_cancelled_via_same_lifecycle(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,llm={'enabled':True,'api_key':'fake','context_mode':'custom'})
    entered = asyncio.Event()
    async def waiting(*args,**kwargs):
        entered.set()
        await asyncio.Event().wait()
    bot.chat.complete = waiting
    task = asyncio.create_task(bot.chat.preview('private',123,'hello'))
    try:
        await asyncio.wait_for(entered.wait(),1)
        turn = (await bot.storage.list_turns())['items'][0]
        assert bot.conversations.cancel(turn['turn_id'])
        with pytest.raises(asyncio.CancelledError):
            await task
        detail = await bot.storage.turn_detail(turn['turn_id'])
        assert detail['status'] == 'cancelled'
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        await bot.stop()
