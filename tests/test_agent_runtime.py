from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        registry={'send':None,'agent__stay_silent':None},execute=execute,max_rounds=3,agent=True)
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
            registry={'send':None},execute=execute,max_rounds=3,agent=True)
    execute.assert_awaited_once()


async def test_empty_response_is_distinct_from_explicit_silence():
    complete = AsyncMock(return_value=SimpleNamespace(text='',tool_calls=[]))
    result = await AgentRuntime(complete).run([],scene='group',scene_id=123,ctx=ctx(),agent=True)
    assert result.reason == 'empty_response'


async def test_tools_remain_available_past_legacy_round_and_batch_limits():
    definitions = [{'type':'function','function':{'name':'read','parameters':{}}}]
    calls = [{'id':str(i),'function':{'name':'read','arguments':'{}'}} for i in range(4)]
    complete = AsyncMock(side_effect=[
        *[SimpleNamespace(text='',tool_calls=calls) for _ in range(7)],
        SimpleNamespace(text='完成',tool_calls=[]),
    ])
    execute = AsyncMock(return_value=('{}',False))
    result = await AgentRuntime(complete).run([],scene='group',scene_id=123,ctx=ctx(),
        tools=definitions,registry={'read':None},execute=execute,max_rounds=3,agent=True)
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
