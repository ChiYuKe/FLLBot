import asyncio
import json
import time

import httpx
import pytest

from fllbot.config import LLMConfig
from fllbot.llm.client import LLMClient, LLMError
from fllbot.logging_setup import clear_ring, get_ring
from helpers import FakeConnection, group_message, make_bot

SECRET = 'PRIVACY_SENTINEL_PRIVATE_BODY'


async def test_protocol_diagnostics_do_not_capture_payloads_or_exception_bodies(caplog):
    from fllbot.onebot.connection import OneBotConnection

    class Socket:
        def __init__(self):
            self.frames = iter([SECRET, json.dumps({'private': SECRET}),
                                json.dumps({'post_type': 'message', 'private': SECRET})])

        async def receive_text(self):
            try:
                return next(self.frames)
            except StopIteration:
                raise OSError(SECRET) from None

    async def on_event(connection, payload):
        raise ValueError(SECRET)

    caplog.set_level('DEBUG', logger='fllbot.onebot')
    connection = OneBotConnection(Socket(), on_event=on_event)
    await connection.read_loop()
    assert connection.events_received == 1
    assert '非法 JSON' in caplog.text and 'ValueError' in caplog.text and 'OSError' in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize('kind',['http','schema','network'])
async def test_upstream_echo_never_enters_errors_logs_or_usage(tmp_path,monkeypatch,kind):
    import fllbot.llm.client as client_module
    monkeypatch.setattr(client_module,'TRANSIENT_RETRY_DELAYS',(0,))
    bot = await make_bot(tmp_path,builtin=False,llm={'enabled':True,'api_key':'fake','context_window':8192})
    def response(request):
        if kind == 'network':
            raise httpx.ReadError(SECRET,request=request)
        return httpx.Response(401 if kind=='http' else 200,json={'error':{'message':SECRET}})
    bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(response))
    clear_ring()
    try:
        with pytest.raises(LLMError) as caught:
            await bot.chat.test(SECRET)
        assert SECRET not in str(caught.value)
        assert SECRET not in str(bot.chat.status())
        assert SECRET not in json.dumps(get_ring())
        rows = await (await bot.storage.db.execute('SELECT error FROM llm_usage')).fetchall()
        assert len(rows) == 1 and SECRET not in str([dict(row) for row in rows])
    finally:
        await bot.stop()


async def test_stream_errors_do_not_echo_response_body():
    config = LLMConfig(api_key='fake',context_window=8192)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(401,text=SECRET))) as http:
        client = LLMClient(lambda:config,http_client=http)
        with pytest.raises(LLMError) as caught:
            async for _ in client.chat_stream([{'role':'user','content':'hello'}]):
                pass
        assert SECRET not in str(caught.value) and '401' in str(caught.value)


async def test_diagnostic_body_requires_optin_and_expires_without_removing_counts(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,overrides={'logging':{'message_content_hours':1}})
    try:
        connection = FakeConnection()
        await bot.on_raw_event(connection,group_message(SECRET,message_id=1))
        await asyncio.wait_for(bot.dispatcher._queue.join(),1)
        assert (await (await bot.storage.db.execute('SELECT content FROM message_log')).fetchone())[0] == ''
        await bot.update_config({'logging':{'capture_message_content':True}})
        await bot.on_raw_event(connection,group_message(SECRET,message_id=2))
        await asyncio.wait_for(bot.dispatcher._queue.join(),1)
        rows = await (await bot.storage.db.execute('SELECT content FROM message_log ORDER BY id')).fetchall()
        assert [row[0] for row in rows] == ['',SECRET]
        await bot.storage.db.execute('UPDATE message_log SET created_at=?',(time.time()-7200,))
        await bot.storage.db.commit()
        assert await bot.storage.expire_message_content(1) == 1
        assert await bot.storage.total_messages() == 2
        assert all(not row[0] for row in await (await bot.storage.db.execute('SELECT content FROM message_log')).fetchall())
    finally:
        await bot.stop()


async def test_generated_admin_token_is_not_logged(tmp_path):
    clear_ring()
    bot = await make_bot(tmp_path,builtin=False,overrides={'admin':{'token':''}})
    try:
        assert bot.admin_token and bot.admin_token not in json.dumps(get_ring())
    finally:
        await bot.stop()


async def test_invalid_usage_is_estimated_without_echoing_values():
    config = LLMConfig(api_key='fake',context_window=8192)
    payload = {'choices':[{'message':{'content':'answer'}}],
               'usage':{'prompt_tokens':SECRET,'completion_tokens':True}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=payload))) as http:
        result = await LLMClient(lambda:config,http_client=http).chat_result([{'role':'user','content':'question'}])
    assert result.usage['estimated'] and result.usage['prompt_tokens'] > 0
    assert SECRET not in str(result.usage)
