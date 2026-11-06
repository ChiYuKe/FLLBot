from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.llm.agent_runtime import AgentRuntime


def ctx():
    return SimpleNamespace(data_current=True, self_id=11, tool_call_id='')


async def test_silence_prevents_all_other_tool_actions():
    calls = [
        {'id':'effect','function':{'name':'send','arguments':'{}'}},
        {'id':'quiet','function':{'name':'agent__stay_silent','arguments':'{}'}},
    ]
    complete = AsyncMock(return_value=SimpleNamespace(text='narration',tool_calls=calls))
    execute = AsyncMock(return_value=('{}',True))
    result = await AgentRuntime(complete).run([],scene='group',scene_id=123,ctx=ctx(),
        registry={'send':None,'agent__stay_silent':None},execute=execute,agent=True)
    assert result.reason == 'explicit_silence'
    execute.assert_awaited_once()
    assert execute.call_args.args[1] == 'agent__stay_silent'


async def test_tool_success_then_model_failure_never_reexecutes_tool():
    complete = AsyncMock(side_effect=[SimpleNamespace(text='',tool_calls=[
        {'id':'call','function':{'name':'send','arguments':'{}'}}]), RuntimeError('model failed')])
    execute = AsyncMock(return_value=('{}',False))
    import pytest
    with pytest.raises(RuntimeError):
        await AgentRuntime(complete).run([],scene='group',scene_id=123,ctx=ctx(),
            registry={'send':None},execute=execute,agent=True)
    execute.assert_awaited_once()


async def test_empty_response_is_distinct_from_explicit_silence():
    complete = AsyncMock(return_value=SimpleNamespace(text='',tool_calls=[]))
    result = await AgentRuntime(complete).run([],scene='group',scene_id=123,ctx=ctx(),agent=True)
    assert result.reason == 'empty_response'


async def test_tools_remain_available_through_long_multi_tool_conversations():
    definitions = [{'type':'function','function':{'name':'read','parameters':{}}}]
    calls = [{'id':str(i),'function':{'name':'read','arguments':'{}'}} for i in range(4)]
    complete = AsyncMock(side_effect=[
        *[SimpleNamespace(text='',tool_calls=calls) for _ in range(7)],
        SimpleNamespace(text='完成',tool_calls=[]),
    ])
    execute = AsyncMock(return_value=('{}',False))
    result = await AgentRuntime(complete).run([],scene='group',scene_id=123,ctx=ctx(),
        tools=definitions,registry={'read':None},execute=execute,agent=True)
    assert result.text == '完成'
    assert execute.await_count == 28
    assert all(call.kwargs['tools'] == definitions for call in complete.await_args_list)


async def test_unlimited_tools_stop_when_context_is_cleared():
    context = ctx()
    complete = AsyncMock(return_value=SimpleNamespace(text='',tool_calls=[
        {'id':'call','function':{'name':'read','arguments':'{}'}}]))
    async def execute(*args):
        context.data_current = False
        return '{}',False
    result = await AgentRuntime(complete).run([],scene='group',scene_id=123,ctx=context,
        registry={'read':None},execute=execute,agent=True)
    assert result.reason == 'context_cleared'
    complete.assert_awaited_once()


@pytest.mark.parametrize('bad', [
    {'function': 'bad'}, {'function': []}, {'function': {'name': ''}},
    {'id': 123, 'function': {'name': 'read'}},
    {'id': '', 'function': {'name': 'read'}},
    {'type': 'unknown', 'function': {'name': 'read'}},
    {'function': {'name': 'read', 'arguments': []}}, 123,
])
async def test_invalid_batch_never_executes_earlier_valid_call(bad):
    calls = [{'id': 'valid', 'function': {'name': 'send'}}, bad]
    complete = AsyncMock(return_value=SimpleNamespace(text='', tool_calls=calls))
    execute = AsyncMock()
    messages = []
    result = await AgentRuntime(complete).run(messages, scene='group', scene_id=123,
        ctx=ctx(), agent=True, execute=execute)
    assert result.reason == 'invalid_tool_call'
    assert messages == []
    execute.assert_not_awaited()


async def test_missing_id_is_shared_by_execution_and_transcript():
    original = {'function': {'name': 'read', 'arguments': {'count': 1}}}
    complete = AsyncMock(side_effect=[SimpleNamespace(text='', tool_calls=[original]),
                                     SimpleNamespace(text='完成', tool_calls=[])])
    ids = []
    async def execute(context, name, arguments, registry):
        ids.append(context.tool_call_id)
        assert arguments == '{"count": 1}'
        return '{}', False
    messages = []
    await AgentRuntime(complete).run(messages, scene='group', scene_id=123,
        ctx=ctx(), agent=True, execute=execute)
    assert ids[0]
    assert messages[0]['tool_calls'][0]['id'] == messages[1]['tool_call_id'] == ids[0]
    assert 'id' not in original


async def test_duplicate_ids_are_rejected_before_execution():
    calls = [{'id': 'same', 'function': {'name': name}} for name in ['read', 'send']]
    complete = AsyncMock(return_value=SimpleNamespace(text='', tool_calls=calls))
    execute = AsyncMock()
    result = await AgentRuntime(complete).run([], scene='group', scene_id=123,
        ctx=ctx(), agent=True, execute=execute)
    assert result.reason == 'invalid_tool_call'
    execute.assert_not_awaited()
