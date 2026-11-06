import asyncio
import json

import httpx
import pytest

from fllbot.config import LLMConfig
from fllbot.llm.budget import fit_messages, request_tokens
from fllbot.llm.client import LLMClient
from fllbot.llm.context_builder import ContextBuilder
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from fllbot.storage import Storage
from helpers import FakeConnection, group_message, make_bot


async def test_snapshot_freezes_tools_and_rules_before_context_io(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    entered,release = asyncio.Event(),asyncio.Event()
    tools = [{'type':'function','function':{'name':'read','parameters':{'type':'object','properties':{}}}}]
    async def assemble(*args,**kwargs):
        entered.set()
        await release.wait()
        return [{'role':'system','content':'persona'},{'role':'user','content':'question'}]
    builder = ContextBuilder(assemble,lambda:bot.config,lambda *args:('default','model'))
    task = asyncio.create_task(builder.build('group',123,'question',tools=tools,system_rules=' runtime rules'))
    try:
        await asyncio.wait_for(entered.wait(),1)
        tools[0]['function']['parameters']['properties']['late'] = {'type':'string'}
        release.set()
        snapshot = await task
        assert snapshot.messages()[0]['content'] == ' runtime rules\n\npersona'
        assert snapshot.tools()[0]['function']['parameters']['properties'] == {}
        copy = snapshot.tools()
        copy[0]['function']['name'] = 'changed'
        assert snapshot.tools()[0]['function']['name'] == 'read'
        assert snapshot.sources()[-1] == {'index':1,'kind':'current','scene':'group','scene_id':123,'self_id':0}
        assert snapshot.request_version
    finally:
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        await bot.stop()


async def test_turn_keeps_only_snapshot_metadata_across_clear_and_restart(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,llm={'api_key':'private-key','context_window':8192})
    ctx = Context(bot,FakeConnection(),parse_event(group_message('private-message')))
    ctx.turn_input = ctx.text
    try:
        async with bot.conversations.turn(ctx):
            await bot.chat.build_messages('group',ctx.group_id,ctx.text,ctx=ctx,include_history=False,
                system_rules='private-rules',tools=[{'type':'function','function':{'name':'read',
                    'description':'private-description','parameters':{'type':'object'}}}])
            detail = await bot.storage.turn_detail(ctx.turn_id)
            metadata = detail['context_metadata']
            assert metadata == ctx.context_snapshot.metadata()
            assert metadata['tool_names'] == ['read']
            assert metadata['source_counts'] == {'system':1,'current':1}
            assert metadata['budget']['providers'][0]['window'] == 8192
            assert 'private-' not in json.dumps(metadata)
        await bot.storage.clear_chat('group',ctx.group_id,self_id=ctx.self_id)
    finally:
        await bot.stop()
    storage = Storage(bot.storage.path)
    await storage.connect()
    try:
        assert (await storage.turn_detail(ctx.turn_id))['context_metadata'] == metadata
        assert (await (await storage.db.execute('SELECT count(*) FROM turn_messages')).fetchone())[0] == 0
    finally:
        await storage.close()


def summary(body):
    prefix = '较早话题摘要（仅背景；来源消息 1—8）：'
    return {'role':'user','content':prefix+body,
            '_fllbot_source':{'kind':'summary','first_id':1,'last_id':8,'prefix':prefix}}


def test_budget_drops_raw_history_before_shortening_summary_and_preserves_current():
    messages = [{'role':'system','content':'rules'},summary('摘要'*1000),
                {'role':'user','content':'old raw'*1000}, {'role':'assistant','content':'old response'},
                {'role':'user','content':'current question'}]
    fitted = fit_messages(messages,None,2200,200)
    assert request_tokens(fitted)+200 <= 2200
    assert fitted[-1] == messages[-1]
    assert not any('old raw' in (item.get('content') or '') for item in fitted)
    assert fitted[1]['_fllbot_source']['kind'] == 'summary'
    assert '来源消息 1—8' in fitted[1]['content'] and '已缩减' in fitted[1]['content']
    assert messages[1]['content'].endswith('摘要'*1000)


async def test_fallback_rechecks_schema_images_output_and_removes_host_metadata(monkeypatch):
    import fllbot.llm.client as client_module
    monkeypatch.setattr(client_module,'TRANSIENT_RETRY_DELAYS',())
    tools = [{'type':'function','function':{'name':'read','description':'schema'*40,'parameters':{'type':'object'}}}]
    messages = [{'role':'system','content':'rules'},summary('背景'*3000),
                {'role':'user','content':'old raw'*2000},
                {'role':'user','content':[{'type':'text','text':'current question'},
                    {'type':'image_url','image_url':{'url':'https://example.test/image'}}]}]
    config = LLMConfig(api_key='fake',default='primary',fallback_providers=['backup'],max_tokens=200,
        providers={'primary':{'base_url':'https://primary.test','context_window':16000,'image_token_budget':1000},
                   'backup':{'base_url':'https://backup.test','context_window':3200,'image_token_budget':1000}})
    captured = []
    def handler(request):
        payload = json.loads(request.content)
        captured.append((request.url.host,payload))
        if request.url.host == 'primary.test':
            return httpx.Response(503,json={'error':{'message':'temporary'}})
        return httpx.Response(200,json={'choices':[{'message':{'content':'answer'},'finish_reason':'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await LLMClient(lambda:config,http_client=http).chat_result(messages,tools=tools)
    assert result.text == 'answer'
    assert [host for host,_ in captured] == ['primary.test','backup.test']
    for host,payload in captured:
        window = 16000 if host == 'primary.test' else 3200
        assert request_tokens(payload['messages'],payload['tools'],1000)+payload['max_tokens'] <= window
        assert payload['messages'][-1] == messages[-1]
        assert not any(any(key.startswith('_fllbot_') for key in item) for item in payload['messages'])


@pytest.mark.parametrize('rules,tools', [('规则'*5000,[]),('',[{'description':'schema'*5000}])],ids=['rules','tools'])
async def test_final_gate_rejects_oversized_rules_and_tools_without_network(rules,tools):
    from fllbot.llm.client import LLMError
    config = LLMConfig(api_key='fake',context_window=1000,max_tokens=200)
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:requests.append(request))) as http:
        with pytest.raises(LLMError,match='上下文预算'):
            await LLMClient(lambda:config,http_client=http).chat_result(
                [{'role':'system','content':rules},{'role':'user','content':'current'}],tools=tools)
    assert not requests
