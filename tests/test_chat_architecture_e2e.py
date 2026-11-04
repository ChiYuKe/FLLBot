"""Whole receive-to-receipt acceptance with simulated model and OneBot only."""
import asyncio
import json

import httpx

from fllbot.onebot.connection import ActionError
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


async def test_raw_events_link_trigger_turn_model_tool_and_partial_delivery(tmp_path):
    bot = await make_bot(tmp_path,llm={'enabled':True,'api_key':'fake','context_window':16000},
        overrides={'chat':{'reply_mode':'split','reply_chunk_size':10,'reply_interval_ms':0}})
    calls = {11:0,22:0}
    def model(request):
        payload = json.loads(request.content)
        user = next(message for message in reversed(payload['messages']) if message['role']=='user')['content']
        account = 11 if 'INPUT_11' in user else 22
        calls[account] += 1
        message = {'content':f'ANSWER_{account}_first|second|last'}
        if calls[account] == 1:
            message = {'content':'','tool_calls':[{'id':'same-call','type':'function',
                'function':{'name':'basic__check_online','arguments':'{}'}}]}
        return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':'stop'}],
            'usage':{'prompt_tokens':20,'completion_tokens':10}})
    bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(model))
    class PartialConnection(FakeConnection):
        async def send_group_msg(self,*args,**kwargs):
            if self.sent:
                raise ActionError('send_group_msg',retcode=1400)
            return await super().send_group_msg(*args,**kwargs)
    connections = {11:FakeConnection(self_id=11),22:PartialConnection(self_id=22)}
    try:
        await bot.permissions.save_group(20002,enabled=True,config={'policy':{
            'auto_group':True,'trigger_modes':['all'],'cooldown_seconds':0,'allowed_tools':['basic__check_online']}})
        for account in (11,11,22):
            payload = group_message(f'INPUT_{account}',message_id=77,at=account)
            payload['self_id'] = account
            await bot.on_raw_event(connections[account],payload)
        await asyncio.wait_for(bot.dispatcher._queue.join(),3)
        await bot.traces.flush()
        events = (await bot.storage.list_message_events())['items']
        assert len(events) == 2 and calls == {11:2,22:2}
        assert {event['self_id'] for event in events} == {11,22}
        assert all(event['status']=='completed' for event in events)
        for event in events:
            account = event['self_id']
            detail = await bot.storage.turn_detail(event['turn_id'])
            assert detail['self_id'] == account and detail['scene_id'] == 20002
            assert detail['trace_id'] == event['trace_id'] and detail['trace_id']
            assert detail['deadline'] == event['deadline'] and detail['data_epoch'] == event['data_epoch']
            assert detail['scope_epoch'] == event['scope_epoch']
            assert detail['status'] == ('delivered' if account==11 else 'failed')
            assert len(detail['model_requests']) == 2
            assert [(tool['call_id'],tool['status']) for tool in detail['tools']] == [('same-call','succeeded')]
            assert detail['context_metadata']['request_version']
            trigger = await (await bot.storage.db.execute('SELECT * FROM trigger_decisions WHERE turn_id=?',
                (event['turn_id'],))).fetchone()
            assert trigger['outcome'] == 'accept' and trigger['effective_policy_version']
            source = await (await bot.storage.db.execute('SELECT self_id,content FROM turn_messages WHERE turn_id=?',
                (event['turn_id'],))).fetchone()
            assert source['self_id'] == account and f'INPUT_{account}' in source['content']
            trace = await (await bot.storage.db.execute('SELECT * FROM message_traces WHERE trace_id=?',
                (event['trace_id'],))).fetchone()
            assert trace is not None
            timings = detail['trace_metrics']
            assert 0 <= timings['queue_wait_ms'] <= timings['first_model_response_ms'] <= timings['first_delivery_ms']
            assert timings['first_delivery_ms'] <= trace['duration_ms']
            parts = await (await bot.storage.db.execute(
                'SELECT p.* FROM delivery_parts p JOIN reply_deliveries d USING(delivery_id) WHERE d.turn_id=? ORDER BY p.part_index',
                (event['turn_id'],))).fetchall()
            expected = [part['content'] for part in parts if part['status']=='sent']
            history = await bot.storage.recent_chat('group',20002,self_id=account)
            assert [item.content for item in history if item.role=='assistant'] == expected
            assert all(part['message_id'] for part in parts if part['status']=='sent')
            if account==22:
                assert detail['reason'] == 'partial_delivery'
                assert [part['status'] for part in parts][:2] == ['sent','failed']
                following = group_message('next request',message_id=78)
                following['self_id'] = account
                ctx = Context(bot,connections[account],parse_event(following))
                context = await bot.chat.build_messages('group',20002,ctx.text,ctx=ctx)
                assert [message['content'] for message in context if message['role']=='assistant'] == expected
                assert not any('INPUT_11' in str(message['content']) for message in context)
        assert 'INPUT_' not in str(await bot.storage.list_turns())
        for account,rate in [(11,1.0),(22,0.5)]:
            metrics = (await bot.storage.list_turns(self_id=account))['delivery_metrics']
            assert metrics['confirmed_delivery_rate'] == rate
            assert metrics['uncertain_tools'] == 0
    finally:
        await bot.stop()


async def test_raw_model_failure_has_distinct_reason_and_no_delivery(tmp_path):
    bot = await make_bot(tmp_path,llm={'enabled':True,'api_key':'fake','context_window':8192})
    bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(401,json={'error':{'message':'denied'}})))
    connection = FakeConnection()
    try:
        await bot.permissions.save_group(20002,enabled=True,config={'policy':{
            'auto_group':True,'trigger_modes':['all'],'cooldown_seconds':0}})
        await bot.on_raw_event(connection,group_message('model failure',at=10001))
        await asyncio.wait_for(bot.dispatcher._queue.join(),3)
        event = (await bot.storage.list_message_events())['items'][0]
        detail = await bot.storage.turn_detail(event['turn_id'])
        assert (detail['status'],detail['reason']) == ('failed','model_failed')
        assert len(detail['model_requests']) == 1 and not detail['model_requests'][0]['ok']
        assert not detail['deliveries'] and not connection.sent
    finally:
        await bot.stop()
