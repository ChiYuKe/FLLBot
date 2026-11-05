import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.models import parse_event
from fllbot.plugin.base import AIToolSpec
from fllbot.plugin.context import Context
from fllbot.tool_executor import encode_result
from helpers import FakeConnection, group_message, make_bot


@pytest.mark.parametrize('limit', [64, 100, 8000])
@pytest.mark.parametrize('result', ['"\\\n\t' * 5000, {'中文': ['长文本' * 10000] * 2}, list(range(10000))],
                         ids=['escaped', 'unicode', 'array'])
def test_serialized_tool_result_respects_limit(result, limit):
    output = encode_result(result, limit)
    assert len(output) <= limit
    assert json.loads(output)['truncated'] is True
    assert json.loads(output)['preview']


def test_small_tool_result_keeps_original_shape():
    result = {'items': [1, '中文', True], 'silent': False}
    assert json.loads(encode_result(result, 8000)) == result


@pytest.mark.parametrize('option', [
    {'effect':'unknown'}, {'timeout':float('nan')}, {'timeout':float('inf')},
    {'timeout':0}, {'timeout':True}, {'max_output_chars':63},
    {'max_output_chars':64.5}, {'max_output_chars':True},
])
def test_invalid_tool_execution_limits_rejected(option):
    with pytest.raises(ValueError):
        AIToolSpec('test', '', {'type':'object'}, AsyncMock(), **option)


async def test_truncated_result_is_persisted_and_reused(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        owner = SimpleNamespace(name='example')
        await bot.permissions.set_plugin_enabled('example', True)
        callback = AsyncMock(return_value={'content':'"\\\n' * 10000})
        spec = AIToolSpec('read', '', {'type':'object'}, callback, plugin_name='example',
                          effect='read_only', max_output_chars=100)
        ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        ctx.tool_call_id = 'large-result'
        registry = {spec.qualified_name:(owner,spec)}
        output, silent = await bot.tool_executor.execute(ctx, spec.qualified_name, {}, registry)
        assert len(output) <= 100 and json.loads(output)['truncated']
        assert not silent
        assert await bot.tool_executor.execute(ctx, spec.qualified_name, {}, registry) == (output, silent)
        callback.assert_awaited_once()
        row = await (await bot.storage.db.execute('SELECT content FROM tool_results')).fetchone()
        assert row['content'] == output
    finally:
        await bot.stop()


async def test_concurrent_claims_do_not_exceed_callback_capacity(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = []
    try:
        owner = SimpleNamespace(name='example')
        await bot.permissions.set_plugin_enabled('example', True)
        bot.tool_executor.max_callbacks = 1
        calls = []
        async def callback(ctx):
            calls.append(ctx.tool_call_id)
            entered.set()
            await release.wait()
            return {'done':True}
        spec = AIToolSpec('read', '', {'type':'object'}, callback, plugin_name='example',
                          effect='read_only')
        contexts = [Context(bot, FakeConnection(), parse_event(group_message('hello'))) for _ in range(8)]
        for index,ctx in enumerate(contexts):
            ctx.tool_call_id = str(index)
        tasks = [asyncio.create_task(bot.tool_executor.execute(ctx, spec.qualified_name, {},
            {spec.qualified_name:(owner,spec)})) for ctx in contexts]
        await asyncio.wait_for(entered.wait(),1)
        active = int(calls[0])
        results = await asyncio.wait_for(asyncio.gather(*(task for index,task in enumerate(tasks) if index != active)),1)
        assert len(calls) == 1
        codes = [json.loads(output)['error'] for output,_ in results]
        assert codes.count('tool_executor_busy') == 7
        release.set()
        assert json.loads((await tasks[active])[0]) == {'done':True}
    finally:
        release.set()
        await asyncio.gather(*tasks,return_exceptions=True)
        await bot.stop()


async def test_read_only_timeout_is_failed_not_uncertain(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        await bot.permissions.set_plugin_enabled('example',True)
        async def callback(ctx):
            await asyncio.Event().wait()
        spec = AIToolSpec('read', '', {'type':'object'}, callback, plugin_name='example',
                          effect='read_only', timeout=0.01)
        ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        output,_ = await bot.tool_executor.execute(ctx, spec.qualified_name, {},
            {spec.qualified_name:(SimpleNamespace(name='example'),spec)})
        assert json.loads(output)['error'] == 'tool_failed'
        row = await (await bot.storage.db.execute('SELECT effect,status FROM tool_executions')).fetchone()
        assert (row['effect'],row['status']) == ('read_only','failed')
    finally:
        await bot.stop()


@pytest.mark.parametrize(('arguments', 'constraint'), [
    ('{', 'json'), ('{"count":true}', 'type'), ('{"count":-1}', 'minimum'),
    ('{"count":1,"extra":0}', 'additionalProperties'), ('{}', 'required'),
    ('{"count":"PRIVATE_ARGUMENT"}', 'type'), ('[]', 'object'),
])
async def test_invalid_schema_never_executes(tmp_path, arguments, constraint):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        owner = SimpleNamespace(name='example')
        await bot.permissions.set_plugin_enabled('example', True)
        callback = AsyncMock()
        spec = AIToolSpec('count', '', {'type':'object', 'properties': {'count': {'type':'integer', 'minimum':0}},
                                     'required':['count']}, callback, plugin_name='example')
        ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        output, silent = await bot.tool_executor.execute(ctx, spec.qualified_name, arguments, {spec.qualified_name:(owner,spec)})
        assert json.loads(output)['error'] == 'invalid_arguments'
        details = json.loads(output)['details']
        assert details['constraint'] == constraint
        if constraint in ('type', 'minimum'):
            assert details['path'] == ['count']
        if constraint == 'required':
            assert details['expected'] == ['count']
        assert 'PRIVATE_ARGUMENT' not in output
        callback.assert_not_awaited()
        assert not silent
    finally:
        await bot.stop()


async def test_side_effect_timeout_is_uncertain_and_not_retried(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        owner = SimpleNamespace(name='example')
        await bot.permissions.set_plugin_enabled('example', True)
        calls = []
        async def callback(ctx):
            calls.append(ctx.tool_execution_id)
            await asyncio.Event().wait()
        spec = AIToolSpec('send', '', {'type':'object'}, callback, plugin_name='example', timeout=0.01)
        ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        output, _ = await bot.tool_executor.execute(ctx, spec.qualified_name, {}, {spec.qualified_name:(owner,spec)})
        assert json.loads(output)['error'] == 'tool_uncertain'
        rows = await (await bot.storage.db.execute('SELECT execution_id,status FROM tool_executions')).fetchall()
        assert [(row['execution_id'],row['status']) for row in rows] == [(calls[0], 'uncertain')]
        assert ctx.plugin is None and ctx.tool_execution_id == ''
    finally:
        await bot.stop()


async def test_success_commits_fact_and_restores_context(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        owner = SimpleNamespace(name='example')
        await bot.permissions.set_plugin_enabled('example', True)
        callback = AsyncMock(return_value={'done': True})
        spec = AIToolSpec('send', '', {'type':'object'}, callback, plugin_name='example')
        ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        output, _ = await bot.tool_executor.execute(ctx, spec.qualified_name, {}, {spec.qualified_name:(owner,spec)})
        assert json.loads(output) == {'done':True}
        row = await (await bot.storage.db.execute('SELECT status FROM tool_executions')).fetchone()
        assert row['status'] == 'succeeded'
        assert ctx.plugin is None
    finally:
        await bot.stop()


async def test_duplicate_call_reuses_result_and_rejects_changed_arguments(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        owner = SimpleNamespace(name='example')
        await bot.permissions.set_plugin_enabled('example', True)
        callback = AsyncMock(return_value={'done':True})
        spec = AIToolSpec('send', '', {'type':'object','properties':{'n':{'type':'integer'}}}, callback, plugin_name='example')
        ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        ctx.tool_call_id = 'call-1'
        registry = {spec.qualified_name:(owner,spec)}
        first = await bot.tool_executor.execute(ctx, spec.qualified_name, {'n':1}, registry)
        second = await bot.tool_executor.execute(ctx, spec.qualified_name, {'n':1}, registry)
        assert first == second
        output, _ = await bot.tool_executor.execute(ctx, spec.qualified_name, {'n':2}, registry)
        assert json.loads(output)['error'] == 'call_identity_conflict'
        callback.assert_awaited_once()
        # Clear result bodies but preserve the action fact; no stale callback replay.
        await bot.storage.clear_chat('group', 20002)
        fresh = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        fresh.tool_call_id = 'call-1'
        output, _ = await bot.tool_executor.execute(fresh, spec.qualified_name, {'n':1}, registry)
        assert json.loads(output)['error'] == 'tool_succeeded'
        callback.assert_awaited_once()
        row = await (await bot.storage.db.execute('SELECT count(*) FROM tool_results')).fetchone()
        assert row[0] == 0
    finally:
        await bot.stop()


async def test_concurrent_duplicate_cannot_execute_twice(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    release = asyncio.Event()
    entered = asyncio.Event()
    try:
        owner = SimpleNamespace(name='example')
        await bot.permissions.set_plugin_enabled('example', True)
        calls = []
        async def callback(ctx):
            calls.append(ctx.tool_execution_id)
            entered.set()
            await release.wait()
            return {'done':True}
        spec = AIToolSpec('send', '', {'type':'object'}, callback, plugin_name='example')
        ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        other = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        ctx.tool_call_id = other.tool_call_id = 'call-1'
        registry = {spec.qualified_name:(owner,spec)}
        task = asyncio.create_task(bot.tool_executor.execute(ctx, spec.qualified_name, {}, registry))
        await asyncio.wait_for(entered.wait(), 1)
        output, _ = await bot.tool_executor.execute(other, spec.qualified_name, {}, registry)
        assert json.loads(output)['error'] == 'tool_running'
        release.set()
        await task
        assert len(calls) == 1
    finally:
        release.set()
        await bot.stop()


async def test_restart_retains_uncertain_and_prevents_reexecution(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        owner = SimpleNamespace(name='example')
        await bot.permissions.set_plugin_enabled('example', True)
        callback = AsyncMock(return_value={'done':True})
        spec = AIToolSpec('send', '', {'type':'object'}, callback, plugin_name='example')
        ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
        ctx.tool_call_id = 'call-1'
        import hashlib
        digest = hashlib.sha256(b'{}').hexdigest()
        claimed, _ = await bot.storage.claim_tool('execution', ctx.self_id, 'group', ctx.group_id,
            ctx.message_id, spec.qualified_name, spec.effect, ctx.data_epoch, ctx.turn_id, 'call-1', digest)
        assert claimed
        await bot.storage.close()
        await bot.storage.connect()
        output, _ = await bot.tool_executor.execute(ctx, spec.qualified_name, {}, {spec.qualified_name:(owner,spec)})
        assert json.loads(output)['error'] == 'tool_uncertain'
        callback.assert_not_awaited()
    finally:
        await bot.stop()
