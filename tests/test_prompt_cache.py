import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from fllbot.config import LLMConfig
from fllbot.llm.client import LLMClient
from fllbot.llm.context_builder import ContextBuilder
from fllbot.storage import Storage
from helpers import FakeConnection, group_message, make_bot


@pytest.mark.parametrize(('usage', 'cached'), [
    ({'prompt_tokens': 1000, 'completion_tokens': 10, 'prompt_cache_hit_tokens': 800}, 800),
    ({'prompt_tokens': 1000, 'completion_tokens': 10, 'prompt_cache_hit_tokens': 0}, 0),
    ({'prompt_tokens': 1000, 'completion_tokens': 10, 'prompt_tokens_details': {'cached_tokens': 500}}, 500),
    ({'prompt_tokens': 1000, 'completion_tokens': 10}, None),
    ({'prompt_tokens': 1000, 'completion_tokens': 10, 'prompt_cache_hit_tokens': 1001}, None),
    ({'prompt_tokens': 1000, 'completion_tokens': 10, 'prompt_cache_hit_tokens': True}, None),
    ({'prompt_tokens': 1000, 'completion_tokens': 10, 'prompt_cache_hit_tokens': -1}, None),
    ({'completion_tokens': 10, 'prompt_cache_hit_tokens': 800}, None),
    ({'prompt_tokens': 1000, 'prompt_cache_hit_tokens': 800}, 800),
])
async def test_provider_cache_counts_preserved_only_when_valid(usage, cached):
    config = LLMConfig(api_key='test', base_url='https://model.test', model='test', context_window=8192)
    def response(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': 'answer'}}], 'usage': usage})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        client = LLMClient(lambda: config, http_client=http)
        result = await client.chat_result([{'role': 'user', 'content': 'question'}])
    assert result.usage.get('cached_prompt_tokens') == cached
    assert ('cached_prompt_tokens' in result.usage) == (cached is not None)


async def test_static_prefix_precedes_dynamic_persona_and_tool_order_is_stable(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        assemble = AsyncMock(side_effect=[
            [{'role': 'system', 'content': 'persona USER_A TIME_1'}, {'role': 'user', 'content': 'one'}],
            [{'role': 'system', 'content': 'persona USER_B TIME_2'}, {'role': 'user', 'content': 'two'}],
        ])
        builder = ContextBuilder(assemble, lambda: bot.config, lambda *args: ('default', 'model'))
        tools = [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}
                 for name in ['z_read', 'a_search']]
        instructions = [{'source': 'z_policy', 'text': 'Z'}, {'source': 'a_policy', 'text': 'A'}]
        first = await builder.build('group', 1, 'one', tools=tools, system_rules='CORE_RULES', agent_instructions=instructions)
        second = await builder.build('group', 1, 'two', tools=list(reversed(tools)), system_rules='CORE_RULES', agent_instructions=list(reversed(instructions)))
        a, b = first.messages()[0]['content'], second.messages()[0]['content']
        assert a.startswith('CORE_RULES\n\n[插件策略 a_policy]')
        assert a[:a.index('persona')] == b[:b.index('persona')]
        assert 'USER_A' in a and 'USER_B' not in a and 'USER_B' in b
        assert first.serialized_tools == second.serialized_tools
    finally:
        await bot.stop()


def test_wire_tool_order_and_schema_keys_are_canonical_without_enabling_other_tools():
    config = LLMConfig()
    client = LLMClient(lambda: config)
    first = [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object', 'properties': {}}}}
             for name in ['z_read', 'a_search']]
    second = [{'function': {'parameters': {'properties': {}, 'type': 'object'}, 'name': name}, 'type': 'function'}
              for name in ['a_search', 'z_read']]
    options = dict(max_tokens=None, temperature=None, stream=False)
    a = client._payload(config.resolve()[1], [], tools=first, **options)
    b = client._payload(config.resolve()[1], [], tools=second, **options)
    assert json.dumps(a['tools']) == json.dumps(b['tools'])
    assert {item['function']['name'] for item in a['tools']} == {'a_search', 'z_read'}
    assert first[0]['function']['name'] == 'z_read'


async def test_cache_statistics_are_weighted_and_unknown_records_excluded(tmp_path):
    store = Storage(tmp_path / 'cache.db')
    await store.connect()
    try:
        for tokens, hits, ok in [(1000, 800, True), (9000, 100, True), (5000, None, True), (100, 50, False)]:
            await store.record_llm_usage(scene='group', scene_id=1, provider='test', model='test',
                prompt_tokens=tokens, completion_tokens=10, latency_ms=1, cached_prompt_tokens=hits, ok=ok)
        summary = await store.llm_usage_summary(0)
        assert summary['prompt_cache'] == {'reported_requests': 2, 'input_tokens': 10000,
                                         'cached_tokens': 900, 'hit_rate': 0.09}
        assert summary['by_model'][0]['prompt_cache'] == summary['prompt_cache']
    finally:
        await store.close()


async def test_cache_schema_migration_keeps_old_counts_unknown(tmp_path, monkeypatch):
    import fllbot.storage.db as module

    path = tmp_path / 'old.db'
    monkeypatch.setattr(module, 'SCHEMA_VERSION', 24)
    store = Storage(path)
    await store.connect()
    await store.db.execute("INSERT INTO llm_usage(scene,scene_id,provider,model,prompt_tokens,completion_tokens,latency_ms,cost,ok,error,created_at) VALUES('group',1,'test','test',100,10,1,0,1,'',0)")
    await store.db.commit()
    await store.close()
    monkeypatch.setattr(module, 'SCHEMA_VERSION', 25)
    await store.connect()
    try:
        row = await (await store.db.execute('SELECT cached_prompt_tokens FROM llm_usage')).fetchone()
        assert row['cached_prompt_tokens'] is None
        assert (await store.llm_usage_summary(0))['prompt_cache']['hit_rate'] is None
    finally:
        await store.close()


async def test_cache_counts_reach_turn_details_and_usage_statistics(tmp_path):
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'test', 'context_window': 16000})
    bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
        json={'choices': [{'message': {'content': 'answer'}}], 'usage': {
            'prompt_tokens': 1000, 'completion_tokens': 10, 'prompt_cache_hit_tokens': 800,
            'prompt_cache_miss_tokens': 200}})))
    try:
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {
            'auto_group': True, 'trigger_modes': ['at'], 'cooldown_seconds': 0}})
        await bot.on_raw_event(FakeConnection(), group_message('question', at=10001, message_id=1))
        await asyncio.wait_for(bot.dispatcher._queue.join(), 2)
        turns = await bot.storage.list_turns()
        detail = await bot.storage.turn_detail(turns['items'][0]['turn_id'])
        assert detail['model_requests'][0]['cached_prompt_tokens'] == 800
        assert detail['prompt_cache']['hit_rate'] == 0.8
        assert (await bot.storage.llm_usage_summary(0))['prompt_cache']['hit_rate'] == 0.8
    finally:
        await bot.stop()
