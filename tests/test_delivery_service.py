"""Initial delivery runs with injected persistence/formatting and no application object."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.config import ChatConfig
from fllbot.delivery_service import DeliveryService
from fllbot.llm.reply import ReplyService
from fllbot.onebot.connection import ActionError
from fllbot.storage import Storage


async def test_standalone_delivery_persists_before_protocol_and_projects_only_success(tmp_path):
    storage = Storage(tmp_path/'delivery.db')
    await storage.connect()
    attempts = []
    after_sent = AsyncMock()
    async def send(segments, **kwargs):
        rows = await (await storage.db.execute('SELECT status FROM delivery_parts ORDER BY part_index')).fetchall()
        index = len(attempts)
        assert rows[index]['status'] == 'sending'
        attempts.append(segments)
        if index:
            raise ActionError('explicit rejection',retcode=1400)
        return {'message_id':'receipt-1'}
    ctx = SimpleNamespace(data_current=True,self_id=11,is_group=True,group_id=123,user_id=456,
        message_id=1,turn_id='standalone',event=SimpleNamespace(scene='group',scene_id=123),
        received=SimpleNamespace(scope_generation=0,account_epoch=0),data_epoch=storage.conversation_generation,
        reply_settings=ChatConfig(reply_mode='split',reply_chunk_size=3,reply_interval_ms=0),
        reply_default_chunk_size=3,memory_message_id=0,plugin=None,handler=None,
        connection=SimpleNamespace(nickname='Bot'),send=send,trace_event=lambda *args:None,
        trace_span=lambda *args:nullcontext())
    service = DeliveryService(storage,None,None,after_sent=after_sent,replies=ReplyService(),allowed=lambda *args:True)
    try:
        with pytest.raises(ActionError):
            await service.send_text(ctx,'abcdef')
        rows = await (await storage.db.execute('SELECT status,message_id FROM delivery_parts ORDER BY part_index')).fetchall()
        assert [(row['status'],row['message_id']) for row in rows] == [('sent','receipt-1'),('failed','')]
        history = await storage.recent_chat('group',123,self_id=11)
        assert [(item.role,item.content) for item in history] == [('assistant','abc')]
        after_sent.assert_awaited_once_with(ctx,'abc')
    finally:
        await storage.close()


async def test_forward_success_cancels_fallback_atomically_and_memory_failure_never_resends(tmp_path):
    storage = Storage(tmp_path/'forward.db')
    await storage.connect()
    forward = AsyncMock(return_value={'message_id':'forward-receipt'})
    after_sent = AsyncMock(side_effect=ActionError('memory failure',retcode=1400))
    ctx = SimpleNamespace(data_current=True,self_id=11,is_group=True,group_id=123,user_id=456,
        message_id=1,turn_id='standalone',event=SimpleNamespace(scene='group',scene_id=123),
        received=SimpleNamespace(scope_generation=0,account_epoch=0),data_epoch=storage.conversation_generation,
        reply_settings=ChatConfig(reply_mode='forward',reply_chunk_size=3,reply_interval_ms=0),
        reply_default_chunk_size=3,memory_message_id=0,plugin=None,handler=None,
        connection=SimpleNamespace(nickname='Bot',send_group_forward_msg=forward),send=AsyncMock(),
        trace_event=lambda *args:None,trace_span=lambda *args:nullcontext())
    service = DeliveryService(storage,None,None,after_sent=after_sent,replies=ReplyService(),allowed=lambda *args:True)
    try:
        await service.send_text(ctx,'abcdef')
        forward.assert_awaited_once()
        ctx.send.assert_not_awaited()
        rows = await (await storage.db.execute('SELECT status FROM delivery_parts ORDER BY part_index')).fetchall()
        assert [row['status'] for row in rows] == ['sent','cancelled','cancelled']
        history = await storage.recent_chat('group',123,self_id=11)
        assert [(item.role,item.content) for item in history] == [('assistant','abcdef')]
    finally:
        await storage.close()
