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


async def test_plain_chat_keeps_tools_visible_without_extra_summary_request():
    definitions = [{'type': 'function', 'function': {'name': 'text__send_markdown_file', 'parameters': {}}}]
    messages = [{'role': 'system', 'content': 'policy'},
                {'role': 'assistant', 'content': '刚刚整理好的说明'},
                {'role': 'user', 'content': '好的，谢谢'}]
    complete = AsyncMock(return_value=SimpleNamespace(text='不客气', tool_calls=[]))
    budget = AsyncMock()
    execute = AsyncMock()
    result = await AgentRuntime(complete, budget).run(messages, scene='group', scene_id=123,
        ctx=ctx(), agent=True, tools=definitions, execute=execute)
    assert result.text == '不客气'
    complete.assert_awaited_once()
    budget.assert_not_awaited()
    execute.assert_not_awaited()
    assert complete.call_args.kwargs['tools'] == definitions
    assert complete.call_args.args[0][1]['content'] == '刚刚整理好的说明'


@pytest.mark.parametrize('failed_result', ['{"error":"tool_uncertain"}', '{"sent":false}',
                                         '{"error":"invalid_arguments"}'])
async def test_mixed_delivery_batch_reports_failure_without_replaying_success(failed_result):
    calls = [{'id': 'ok', 'function': {'name': 'send_image', 'arguments': '{}'}},
             {'id': 'bad', 'function': {'name': 'send_file', 'arguments': '{}'}}]
    complete = AsyncMock(side_effect=[SimpleNamespace(text='', tool_calls=calls),
                                      SimpleNamespace(text='图片已发送，文件尚未完成。', tool_calls=[])])
    execute = AsyncMock(side_effect=[('{"sent":true,"silent":true}', True), (failed_result, False)])
    messages = [{'role': 'user', 'content': '把说明发成图片和文件'}]
    result = await AgentRuntime(complete).run(messages, scene='group', scene_id=123,
        ctx=ctx(), agent=True, execute=execute)
    assert result.text == '图片已发送，文件尚未完成。'
    assert execute.await_count == 2 and complete.await_count == 2
    assert [call.args[1] for call in execute.await_args_list] == ['send_image', 'send_file']
    assert [message['tool_call_id'] for message in messages if message['role'] == 'tool'] == ['ok', 'bad']
    reminders = [message for message in messages if message.get('_fllbot_source', {}).get('kind') == 'tool_failure']
    assert len(reminders) == 1 and '不要重做' in reminders[0]['content']


async def test_all_successful_deliveries_still_finish_without_extra_model_request():
    complete = AsyncMock(return_value=SimpleNamespace(text='', tool_calls=[
        {'id': 'ok', 'function': {'name': 'send', 'arguments': '{}'}}]))
    execute = AsyncMock(return_value=('{"sent":true,"silent":true}', True))
    result = await AgentRuntime(complete).run([], scene='group', scene_id=123,
        ctx=ctx(), agent=True, execute=execute)
    assert result.reason == 'tool_completed'
    complete.assert_awaited_once()
