import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from fllbot.app import create_app
from fllbot.llm.client import LLMClient
from fllbot.logging_setup import record_to_dict
from fllbot.onebot.connection import ActionError, OneBotConnection
from fllbot.onebot.models import parse_event
from fllbot.plugin.base import HandlerSpec
from fllbot.storage import MIGRATIONS, SCHEMA_VERSION, Storage
from fllbot.tracing import MessageTrace, TraceRecorder, activate, record, span
from helpers import FakeConnection, group_message, make_bot

AUTH = {'Authorization': 'Bearer admin-token'}


async def trace_items(bot):
    await bot.traces.flush()
    return (await bot.storage.message_traces())['items']


async def test_success_skips_and_send_failures_are_private_and_persistent(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path)
    conn = FakeConnection()
    try:
        await bot.dispatcher.dispatch(parse_event(group_message('/echo PRIVATE-MESSAGE', message_id=11)), conn)
        await bot.permissions.set_group_enabled(123, False)
        await bot.dispatcher.dispatch(parse_event(group_message('PRIVATE-DENIED', group_id=123, message_id=12)), conn)
        conn.send_group_msg = AsyncMock(side_effect=RuntimeError('PRIVATE-EXCEPTION-SECRET'))
        await bot.dispatcher.dispatch(parse_event(group_message('/echo PRIVATE-FAILED', message_id=13)), conn)
        items = await trace_items(bot)
        assert [item['outcome'] for item in items] == ['failed', 'skipped', 'sent']
        details = [await bot.storage.message_trace(item['trace_id']) for item in items]
        assert any(step['stage'] == 'send' and step['status'] == 'failed' for step in details[0]['steps'])
        assert any(step['code'] == 'scene_denied' for step in details[1]['steps'])
        assert any(step['stage'] == 'handler' for step in details[2]['steps'])
        assert 'PRIVATE' not in json.dumps(details)
        saved = items[-1]['trace_id']
    finally:
        await bot.stop()
    storage = Storage(bot.storage.path)
    await storage.connect()
    try:
        assert (await storage.message_trace(saved))['outcome'] == 'sent'
    finally:
        await storage.close()


async def test_real_model_and_tool_stages_share_trace_without_arguments_or_output(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'PRIVATE-KEY', 'model': 'demo'})
    calls = 0

    def respond(request):
        nonlocal calls
        if request.method == 'GET':
            return httpx.Response(200, json={'data': []})
        calls += 1
        message = {'content': 'PRIVATE-ANSWER'} if calls > 1 else {'content': None, 'tool_calls': [{
            'id': 'PRIVATE-CALL', 'type': 'function', 'function': {'name': 'basic__check_online', 'arguments': '{}'},
        }]}
        return httpx.Response(200, json={'choices': [{'message': message}]})

    bot.chat.client = LLMClient(lambda: bot.config.llm, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    try:
        await bot.plugins.get('agent').settings.update({'auto_group': True, 'trigger_mode': 'at', 'allowed_tools': ['basic__check_online']})
        await bot.dispatcher.dispatch(parse_event(group_message('PRIVATE-PROMPT', at=10001)), FakeConnection())
        items = await trace_items(bot)
        detail = await bot.storage.message_trace(items[0]['trace_id'])
        assert detail['outcome'] == 'sent'
        assert {'model', 'tool', 'send', 'trigger'} <= {step['stage'] for step in detail['steps']}
        assert any(step['component'] == 'basic__check_online' for step in detail['steps'])
        assert 'PRIVATE' not in json.dumps(detail)
        assert calls == 2
    finally:
        await bot.stop()


async def test_trace_api_filters_paginates_and_validates(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path)
    try:
        for number in (1, 2, 3):
            await bot.dispatcher.dispatch(parse_event(group_message('/ping', message_id=number)), FakeConnection())
        await bot.traces.flush()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            assert (await client.get('/api/console/traces')).status_code == 401
            first = (await client.get('/api/console/traces?limit=2&self_id=10001&scene=group&scene_id=20002&outcome=sent', headers=AUTH)).json()
            assert len(first['items']) == 2
            older = (await client.get('/api/console/traces?limit=2&before='+str(first['next_cursor']), headers=AUTH)).json()
            assert len(older['items']) == 1
            assert older['next_cursor'] is None
            filtered = (await client.get('/api/console/traces?message_id=2', headers=AUTH)).json()
            assert len(filtered['items']) == 1
            trace_id = filtered['items'][0]['trace_id']
            assert (await client.get('/api/console/traces/'+trace_id, headers=AUTH)).json()['steps']
            assert (await client.get('/api/console/traces?self_id=0', headers=AUTH)).status_code == 422
            assert (await client.get('/api/console/traces?outcome=bogus', headers=AUTH)).status_code == 422
            assert (await client.get('/api/console/traces/not-a-trace', headers=AUTH)).status_code == 422
            assert (await client.get('/api/console/traces/'+'0'*32, headers=AUTH)).status_code == 404
    finally:
        await bot.stop()


async def test_not_triggered_silence_and_cooldown_are_explained(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake-key', 'model': 'demo'})
    bot.chat.client = LLMClient(lambda: bot.config.llm, http_client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'data': []}) if request.method == 'GET' else httpx.Response(200, json={'choices': [{'message': {'content': '<NO_REPLY>'}}]}),
    )))
    conn = FakeConnection()
    try:
        await bot.plugins.get('agent').settings.update({'auto_group': True, 'trigger_mode': 'at', 'cooldown_seconds': 60})
        for number, at in ((1, None), (2, 10001), (3, 10001)):
            await bot.dispatcher.dispatch(parse_event(group_message('private content', message_id=number, at=at)), conn)
        details = [await bot.storage.message_trace(item['trace_id']) for item in await trace_items(bot)]
        assert all(item['outcome'] == 'skipped' for item in details)
        for item, code in zip(details, ('agent_cooldown', 'model_silent', 'trigger_not_matched'), strict=True):
            assert any(step['code'] == code for step in item['steps'])
        assert conn.sent == []
    finally:
        await bot.stop()


async def test_model_failover_is_visible_even_when_delivery_recovers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr('fllbot.llm.client.TRANSIENT_RETRY_DELAYS', ())
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake-key', 'default': 'main',
                                     'providers': {'main': {'model': 'primary'}, 'backup': {'model': 'secondary'}},
                                     'fallback_providers': ['backup']})

    def respond(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': []})
        if json.loads(request.content)['model'] == 'primary':
            return httpx.Response(503, json={'error': {'message': 'PRIVATE ERROR'}})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'received'}}]})

    bot.chat.client = LLMClient(lambda: bot.config.llm, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    try:
        await bot.plugins.get('agent').settings.update({'auto_group': True, 'trigger_mode': 'at'})
        await bot.dispatcher.dispatch(parse_event(group_message('request', at=10001)), FakeConnection())
        detail = await bot.storage.message_trace((await trace_items(bot))[0]['trace_id'])
        assert detail['outcome'] == 'warning'
        assert any(step['code'] == 'fallback_selected' for step in detail['steps'])
        assert any(step['component'] == 'backup' and step['status'] == 'passed' for step in detail['steps'])
        assert 'PRIVATE' not in json.dumps(detail)
    finally:
        await bot.stop()


async def test_protocol_send_records_echo_success_and_error_without_payloads() -> None:
    class Socket:
        failed = False

        async def send_text(self, text):
            payload = json.loads(text)
            await self.connection._handle_payload({'echo': payload['echo'], 'status': 'failed' if self.failed else 'ok',
                                                    'retcode': 100 if self.failed else 0, 'wording': 'PRIVATE', 'data': {}})

    socket = Socket()
    connection = OneBotConnection(socket, on_event=AsyncMock(), self_id=10001)
    socket.connection = connection
    trace = MessageTrace(parse_event(group_message('PRIVATE')), connection)
    with activate(trace):
        await connection.send_group_msg(20002, 'PRIVATE')
        socket.failed = True
        with pytest.raises(ActionError):
            await connection.send_group_msg(20002, 'PRIVATE')
    result = trace.summary()
    assert result['outcome'] == 'warning'
    assert [step['status'] for step in result['steps']] == ['passed', 'failed']
    assert all(step['stage'] == 'protocol_send' for step in result['steps'])
    assert 'PRIVATE' not in json.dumps(result)


async def test_duplicate_and_queue_overflow_have_terminal_records(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, overrides={'plugin': {'max_pending_events': 1, 'max_concurrency': 1}})
    try:
        conn = FakeConnection()
        payload = group_message('/ping', message_id=99)
        await bot.on_raw_event(conn, payload)
        await bot.on_raw_event(conn, payload)
        await bot.dispatcher._queue.join()
        # No await between emit calls: the first occupies the one-item queue.
        bot.dispatcher.emit(parse_event(group_message('/ping', message_id=100)), conn)
        bot.dispatcher.emit(parse_event(group_message('/ping', message_id=101)), conn)
        await bot.dispatcher._queue.join()
        details = [await bot.storage.message_trace(item['trace_id']) for item in await trace_items(bot)]
        assert any(any(step['code'] == 'duplicate_message' for step in item['steps']) for item in details)
        assert any(item['outcome'] == 'dropped' and item['message_id'] == 101 for item in details)
        assert len(conn.sent) == 2
    finally:
        await bot.stop()


async def test_cancelled_dispatch_and_handler_timeout_are_distinct(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path)
    entered = asyncio.Event()

    async def wait(ctx):
        entered.set()
        await asyncio.Event().wait()

    spec = HandlerSpec(kind='message', name='waiting', plugin_name='basic', callback=wait, priority=0)
    bot.dispatcher.register(spec)
    try:
        task = asyncio.create_task(bot.dispatcher.dispatch(parse_event(group_message('wait', message_id=1)), FakeConnection()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await bot.update_config({'plugin': {'handler_timeout': 0.01}})
        await bot.dispatcher.dispatch(parse_event(group_message('wait', message_id=2)), FakeConnection())
        items = await trace_items(bot)
        assert items[0]['outcome'] == 'failed'
        assert items[1]['outcome'] == 'cancelled'
    finally:
        await bot.stop()


async def test_diagnostics_failure_does_not_break_message_delivery_and_queue_is_bounded(tmp_path: Path, monkeypatch) -> None:
    bot = await make_bot(tmp_path)
    try:
        monkeypatch.setattr(bot.storage, 'record_message_trace', AsyncMock(side_effect=OSError('PRIVATE-STORAGE')))
        conn = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message('/ping')), conn)
        await bot.traces.flush()
        assert conn.texts() == ['pong']
        assert bot.traces.write_failures == 1
        recorder = TraceRecorder(bot.storage)
        for number in range(501):
            recorder.submit(MessageTrace(parse_event(group_message('/ping', message_id=number)), conn))
        assert recorder.queue.qsize() == 500
        assert recorder.dropped == 1
    finally:
        await bot.stop()


def test_trace_steps_are_bounded_and_late_children_cannot_change_finished_snapshot() -> None:
    trace = MessageTrace(parse_event(group_message('PRIVATE')), FakeConnection())
    with activate(trace):
        log = record_to_dict(logging.LogRecord('demo', logging.INFO, '', 1, 'metadata only', (), None))
        assert log['trace_id'] == trace.trace_id
        for _ in range(110):
            record('rule', 'rule_not_matched', 'skipped')
        with pytest.raises(RuntimeError), span('send'):
            raise RuntimeError('PRIVATE')
        result = trace.summary()
        record('handler', 'PRIVATE MESSAGE', component='PRIVATE MESSAGE')
    assert len(result['steps']) == 100
    assert result['truncated'] is True
    assert result['outcome'] == 'failed'
    assert 'PRIVATE' not in json.dumps(result)
    assert 'trace_id' not in record_to_dict(logging.LogRecord('demo', logging.INFO, '', 1, 'after', (), None))


async def test_upgrade_from_seven_and_retention(tmp_path: Path) -> None:
    path = tmp_path/'trace.db'
    with sqlite3.connect(path) as connection:
        for version in sorted(MIGRATIONS):
            if version <= 7:
                for statement in MIGRATIONS[version]:
                    connection.execute(statement)
        connection.execute('PRAGMA user_version=7')
        connection.execute("INSERT INTO groups VALUES(123,1,'{}',0,0)")
    storage = Storage(path)
    await storage.connect()
    try:
        assert await storage.schema_version() == SCHEMA_VERSION
        assert 123 in await storage.list_group_states()
        old = MessageTrace(parse_event(group_message('/ping')), FakeConnection()).summary()
        old['started_at'] = time.time()-8*86400
        await storage.record_message_trace(old)
        assert await storage.message_trace(old['trace_id']) is None
        assert (await storage.message_traces())['items'] == []
        rows = [(f'{i:032x}', 1, 'group', 123, i, time.time(), 0, 'skipped', 0, '[]') for i in range(10001)]
        await storage.db.executemany('INSERT INTO message_traces(trace_id,self_id,scene,scene_id,message_id,started_at,duration_ms,outcome,truncated,steps) VALUES(?,?,?,?,?,?,?,?,?,?)', rows)
        await storage.db.commit()
        fresh = MessageTrace(parse_event(group_message('/ping')), FakeConnection()).summary()
        await storage.record_message_trace(fresh)
        count = await (await storage.db.execute('SELECT count(*) FROM message_traces')).fetchone()
        assert count[0] == 10000
        assert await storage.message_trace('0'*32) is None
        assert await storage.message_trace(f'{10000:032x}') is not None
    finally:
        await storage.close()
