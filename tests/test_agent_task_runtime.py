import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.llm.agent_runtime import AgentRuntime
from fllbot.llm.budget import fit_messages
from fllbot.llm.client import ContextOverflowError, LLMError
from fllbot.llm.task_context import RepeatReminder, TaskContext


def reply(text='', calls=None):
    return SimpleNamespace(text=text, tool_calls=calls or [])


def call(index, name='read', arguments='{}'):
    return {'id': str(index), 'function': {'name': name, 'arguments': arguments}}


def ctx():
    return SimpleNamespace(data_current=True, self_id=11, tool_call_id='')


def messages():
    return [{'role': 'system', 'content': 'SYSTEM'}, {'role': 'user', 'content': 'ORIGINAL GOAL'}]


async def budget(options, entries, tools):
    return {'providers': [{'window': 10000, 'output_reserve': 1000}]}


def test_repeat_reminder_normalizes_arguments_and_resets_on_different_call():
    reminder = RepeatReminder()
    assert not reminder.observe('read', '{"a":1,"b":2}')
    assert not reminder.observe('read', '{"b":2,"a":1}')
    assert '连续 3 次' in reminder.observe('read', '{"a":1,"b":2}')
    assert not reminder.observe('read', '{"a":2}')
    assert reminder.count == 1


async def test_repeated_calls_with_new_ids_receive_advice_after_complete_batch():
    complete = AsyncMock(side_effect=[reply(calls=[call(i)]) for i in range(3)] + [reply('done')])
    execute = AsyncMock(return_value=('{"error":"tool_permission_denied"}', False))
    entries = messages()
    progress = AsyncMock()
    result = await AgentRuntime(complete).run(entries, scene='group', scene_id=1, ctx=ctx(),
        execute=execute, agent=True, progress=progress)
    assert result.text == 'done'
    assert execute.await_count == 3
    reminders = [item for item in entries if item.get('_fllbot_source', {}).get('kind') == 'task_reminder']
    assert len(reminders) == 1
    assert entries[-2]['role'] == 'tool' and entries[-1] == reminders[0]
    assert any(item.args[1] == 'repeat_reminder' for item in progress.await_args_list)


async def test_steering_skips_unstarted_calls_and_preserves_tool_pairs():
    waiting = []
    complete = AsyncMock(side_effect=[reply(calls=[call(1), call(2)]), reply('updated answer')])
    async def execute(*args):
        waiting.append({'role': 'user', 'content': 'NEW REQUIREMENT'})
        return '{"done":true}', False
    async def inbox():
        batch = list(waiting)
        waiting.clear()
        return batch
    entries = messages()
    execution = AsyncMock(side_effect=execute)
    result = await AgentRuntime(complete).run(entries, scene='group', scene_id=1, ctx=ctx(),
        execute=execution, agent=True, inbox=inbox, pending=lambda: bool(waiting))
    assert result.text == 'updated answer'
    execution.assert_awaited_once()
    assert [item['tool_call_id'] for item in entries if item['role'] == 'tool'] == ['1', '2']
    assert json.loads(entries[-2]['content'])['executed'] is False
    assert entries[-1]['content'] == 'NEW REQUIREMENT'


async def test_steering_during_model_request_prevents_old_tools():
    waiting = []
    async def complete(entries, **options):
        if not waiting and not any(item.get('content') == 'NEW' for item in entries):
            waiting.append({'role': 'user', 'content': 'NEW'})
            return reply(calls=[call(1)])
        return reply('updated')
    async def inbox():
        batch = waiting[:]
        waiting.clear()
        return batch
    execute = AsyncMock()
    result = await AgentRuntime(complete).run(messages(), scene='group', scene_id=1, ctx=ctx(),
        execute=execute, agent=True, inbox=inbox, pending=lambda: bool(waiting))
    assert result.text == 'updated'
    execute.assert_not_awaited()


async def test_steering_arriving_during_final_progress_commit_is_not_lost():
    waiting = []
    complete = AsyncMock(side_effect=[reply('old answer'), reply('new answer')])
    async def progress(step, phase, status, *args):
        if step == 1 and phase == 'step' and status == 'succeeded':
            waiting.append({'role': 'user', 'content': 'arrived at final boundary'})
    async def inbox():
        batch = waiting[:]
        waiting.clear()
        return batch
    entries = messages()
    result = await AgentRuntime(complete).run(entries, scene='group', scene_id=1, ctx=ctx(),
        agent=True, inbox=inbox, pending=lambda: bool(waiting), progress=progress)
    assert result.text == 'new answer'
    assert entries[-1]['content'] == 'arrived at final boundary'


async def test_steering_arriving_while_tool_start_is_recorded_prevents_execution():
    waiting = []
    complete = AsyncMock(side_effect=[reply(calls=[call(1)]), reply('updated')])
    async def progress(step, phase, status, *args):
        if step == 1 and phase == 'tool' and status == 'running':
            waiting.append({'role': 'user', 'content': 'new condition'})
    async def inbox():
        batch = waiting[:]
        waiting.clear()
        return batch
    execute = AsyncMock()
    result = await AgentRuntime(complete).run(messages(), scene='group', scene_id=1, ctx=ctx(),
        agent=True, execute=execute, inbox=inbox, pending=lambda: bool(waiting), progress=progress)
    assert result.text == 'updated'
    execute.assert_not_awaited()


def long_history():
    entries = messages()
    for i in range(5):
        entries.extend([{'role': 'assistant', 'content': None, 'tool_calls': [call(i)]},
                        {'role': 'tool', 'tool_call_id': str(i), 'content': 'EVIDENCE' * 1500}])
    return entries


async def test_task_compaction_pins_goal_steering_and_preserves_complete_batches():
    entries = long_history()
    complete = AsyncMock(return_value=reply('已读取资料，已执行动作成功，尚需完成分析。'))
    context = TaskContext(entries, complete, budget)
    entries.append({'role': 'user', 'content': 'KEEP THIS REQUIREMENT',
                    '_fllbot_source': {'kind': 'task_steering'}})
    reduced = await context.compact(entries, {'scene': 'group', 'scene_id': 1}, [])
    assert reduced['after'] < reduced['before']
    assert any(item.get('content') == 'ORIGINAL GOAL' for item in entries)
    assert any(item.get('content') == 'KEEP THIS REQUIREMENT' for item in entries)
    assert any(item.get('_fllbot_source', {}).get('kind') == 'task_summary' for item in entries)
    calls = {item['tool_calls'][0]['id'] for item in entries if item.get('tool_calls')}
    results = {item['tool_call_id'] for item in entries if item['role'] == 'tool'}
    assert calls == results


async def test_summary_failure_prunes_results_without_removing_goal_or_calls():
    entries = long_history()
    context = TaskContext(entries, AsyncMock(side_effect=LLMError('summary failed')), budget)
    reduced = await context.compact(entries, {}, [])
    assert reduced['method'] == 'prune' and reduced['after'] < reduced['before']
    assert len([item for item in entries if item.get('tool_calls')]) == 5
    assert entries[1]['content'] == 'ORIGINAL GOAL'


async def test_overflow_retries_only_model_after_real_reduction_and_never_replays_tool():
    complete = AsyncMock(side_effect=[reply(calls=[call(1)]), ContextOverflowError('overflow'), reply('done')])
    # Below the normal pressure threshold, but force recovery must still shrink.
    async def large_budget(*args):
        return {'providers': [{'window': 100000, 'output_reserve': 1000}]}
    execute = AsyncMock(return_value=('RESULT' * 1000, False))
    progress = AsyncMock()
    result = await AgentRuntime(complete, large_budget).run(messages(), scene='group', scene_id=1, ctx=ctx(),
        execute=execute, agent=True, progress=progress)
    assert result.text == 'done'
    assert complete.await_count == 3
    execute.assert_awaited_once()
    assert any(item.args[1] == 'compaction' for item in progress.await_args_list)


@pytest.mark.parametrize('error', [LLMError('bad auth'), ContextOverflowError('no reducible context')])
async def test_unrecoverable_errors_do_not_retry_or_drop_original_input(error):
    entries = messages()
    original = copy.deepcopy(entries)
    complete = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await AgentRuntime(complete, budget).run(entries, scene='group', scene_id=1, ctx=ctx(), agent=True)
    complete.assert_awaited_once()
    assert [item['content'] for item in entries] == [item['content'] for item in original]


@pytest.mark.parametrize(('error', 'expected'), [
    ({'code': 'context_length_exceeded', 'message': 'PRIVATE_PROMPT'}, ContextOverflowError),
    ({'message': 'maximum context length exceeded PRIVATE_PROMPT'}, ContextOverflowError),
    ({'message': 'invalid api key PRIVATE_PROMPT'}, LLMError),
])
async def test_upstream_context_limits_are_classified_without_exposing_error_body(error, expected):
    import httpx

    from fllbot.config import LLMConfig
    from fllbot.llm.client import LLMClient

    config = LLMConfig(api_key='test', base_url='https://model.test', model='test', context_window=8192)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={'error': error}))) as http:
        client = LLMClient(lambda: config, http_client=http)
        with pytest.raises(expected) as caught:
            await client.chat_result(messages())
        assert type(caught.value) is expected
        assert 'PRIVATE_PROMPT' not in str(caught.value)


def test_final_request_fitting_keeps_original_goal_and_all_applied_steering():
    entries = long_history()
    entries[1]['_fllbot_task_input'] = True
    entries.extend([{'role': 'user', 'content': 'KEEP STEERING',
                     '_fllbot_source': {'kind': 'task_steering'}},
                    {'role': 'assistant', 'content': 'working'},
                    {'role': 'user', 'content': 'REMINDER'}])
    fitted = fit_messages(entries, [], 3000, 500)
    assert any(item.get('content') == 'ORIGINAL GOAL' for item in fitted)
    assert any(item.get('content') == 'KEEP STEERING' for item in fitted)
    assert fitted[-1]['content'] == 'REMINDER'
