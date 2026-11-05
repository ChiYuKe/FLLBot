import asyncio
import json
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock

import pytest

from fllbot.builtin_plugins.agent.plugin import _get_tools
from fllbot.llm.context_builder import ContextBuilder
from fllbot.onebot.models import parse_event
from fllbot.plugin import Context, Plugin, ToolContent, ToolRejection
from fllbot.plugin.agent import instruction_sections
from helpers import FakeConnection, group_message, make_bot


@pytest.fixture
async def extension(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, builtin=False)
    plugin = Plugin('example')
    callback = AsyncMock(return_value={'done': True, 'silent': True})
    plugin.on_ai_tool('send', description='test', parameters={'type': 'object'})(callback)
    plugin.agent_instruction('delivery', tools=['send'], text='test policy')
    await bot.permissions.set_plugin_enabled('example', True)
    loaded = [plugin]
    monkeypatch.setattr(bot.plugins, 'loaded', lambda: list(loaded))
    monkeypatch.setattr(bot.chat, 'turn_agent_settings', lambda *args, **kwargs: {
        'allowed_tools': ['example__send'], 'trigger_modes': ['at'], 'trigger_prefix': ''})
    ctx = Context(bot, FakeConnection(), parse_event(group_message('hello')))
    ctx.tool_call_id = 'call-1'
    spec = plugin.ai_tools[0]
    registry = {spec.qualified_name: (plugin, spec)}
    try:
        yield bot, plugin, callback, ctx, registry, loaded
    finally:
        await bot.stop()


async def test_instruction_follows_whitelist_permission_disable_and_unload(extension, monkeypatch):
    bot, plugin, _, ctx, _, loaded = extension
    def sections():
        definitions, registry = _get_tools(ctx)
        return instruction_sections(registry, definitions)
    assert sections() == [{'source': 'example__delivery', 'text': 'test policy'}]
    settings = bot.chat.turn_agent_settings
    monkeypatch.setattr(bot.chat, 'turn_agent_settings', lambda *args, **kwargs: {
        'allowed_tools': [], 'trigger_modes': ['at'], 'trigger_prefix': ''})
    assert sections() == []
    monkeypatch.setattr(bot.chat, 'turn_agent_settings', settings)
    plugin.ai_tools[0].permission = 'superuser'
    assert sections() == []
    plugin.ai_tools[0].permission = None
    await bot.permissions.set_plugin_enabled('example', False)
    assert sections() == []
    await bot.permissions.set_plugin_enabled('example', True)
    loaded.clear()
    assert sections() == []


async def test_instruction_snapshot_freezes_source_and_text(extension):
    bot, _, _, ctx, registry, _ = extension
    definitions = [{'function': {'name': 'example__send'}}]
    sections = instruction_sections(registry, definitions)
    entered, release = asyncio.Event(), asyncio.Event()
    async def assemble(*args, **kwargs):
        entered.set()
        await release.wait()
        return [{'role': 'system', 'content': 'persona'}, {'role': 'user', 'content': 'hello'}]
    builder = ContextBuilder(assemble, lambda: bot.config, lambda *args: ('default', 'model'))
    task = asyncio.create_task(builder.build('group', 1, 'hello', system_rules=' core',
        agent_instructions=sections))
    await entered.wait()
    sections[0]['text'] = 'changed'
    release.set()
    snapshot = await task
    assert snapshot.messages()[0]['content'] == ' core\n\n[插件策略 example__delivery]\ntest policy\n\npersona'
    assert {'index': 0, 'kind': 'plugin_instruction', 'source': 'example__delivery'} in snapshot.sources()


async def test_pipeline_orders_hooks_persists_transformation_and_reuses_result(extension):
    bot, plugin, callback, ctx, registry, _ = extension
    seen = []
    @plugin.on_tool('before', name='validate')
    async def before(event):
        seen.append('before')
        with pytest.raises(FrozenInstanceError):
            event.tool = 'changed'
    @plugin.on_tool('after', name='format')
    async def after(event):
        assert callback.await_count == 1
        assert event.status == 'succeeded'
        seen.append('after')
        return ToolContent('model result')
    @plugin.on_tool('completed', name='observe')
    async def completed(event):
        seen.append('completed')
        row = await (await bot.storage.db.execute('SELECT status FROM tool_executions')).fetchone()
        assert row['status'] == event.status == 'succeeded'
        assert event.content == 'model result' and event.silent
        raise RuntimeError('observer must not break execution')
    first = await bot.tool_executor.execute(ctx, 'example__send', {}, registry)
    second = await bot.tool_executor.execute(ctx, 'example__send', {}, registry)
    assert first == second == ('model result', True)
    assert seen == ['before', 'after', 'completed', 'before']
    callback.assert_awaited_once()


@pytest.mark.parametrize('mode', ['deny', 'error', 'timeout', 'permission'])
async def test_before_hook_fails_closed_and_cannot_bypass_permissions(extension, mode):
    bot, plugin, callback, ctx, registry, _ = extension
    @plugin.on_tool('before', name='gate', timeout=0.01 if mode == 'timeout' else 1.0)
    async def gate(event):
        if mode == 'deny':
            return ToolRejection('policy denied')
        if mode == 'error':
            raise RuntimeError('failed')
        if mode == 'timeout':
            await asyncio.Event().wait()
        await bot.permissions.set_plugin_enabled('example', False)
    output, silent = await bot.tool_executor.execute(ctx, 'example__send', {}, registry)
    assert json.loads(output)['error'] == {
        'deny': 'tool_rejected', 'error': 'tool_hook_failed', 'timeout': 'tool_hook_failed',
        'permission': 'tool_permission_denied'}[mode]
    callback.assert_not_awaited()
    assert not silent


async def test_after_failure_preserves_fact_and_never_replays_action(extension):
    bot, plugin, callback, ctx, registry, _ = extension
    @plugin.on_tool('after', name='broken')
    async def broken(event):
        raise RuntimeError('format failed')
    first = await bot.tool_executor.execute(ctx, 'example__send', {}, registry)
    assert json.loads(first[0])['error'] == 'tool_result_processing_failed'
    assert first[1]
    assert await bot.tool_executor.execute(ctx, 'example__send', {}, registry) == first
    callback.assert_awaited_once()
    row = await (await bot.storage.db.execute('SELECT status FROM tool_executions')).fetchone()
    assert row['status'] == 'succeeded'


async def test_disabled_or_unloaded_hook_is_skipped(extension):
    bot, plugin, _, ctx, registry, loaded = extension
    observer = Plugin('observer')
    callback = AsyncMock()
    observer.on_tool('completed', name='observe')(callback)
    await bot.permissions.set_plugin_enabled('observer', True)
    loaded.append(observer)
    @plugin.on_tool('before', name='unload')
    async def unload(event):
        loaded.remove(observer)
    await bot.tool_executor.execute(ctx, 'example__send', {}, registry)
    callback.assert_not_awaited()


async def test_hook_filter_order_and_result_bounds(extension):
    bot, plugin, _, ctx, registry, _ = extension
    seen = []
    @plugin.on_tool('before', name='z_last', tools=['send'])
    async def last(event):
        seen.append('last')
    @plugin.on_tool('before', name='a_first', tools=['example__send'])
    async def first(event):
        seen.append('first')
    @plugin.on_tool('before', name='excluded', tools=['other'])
    async def excluded(event):
        pytest.fail('unrelated hook')
    @plugin.on_tool('after', name='large')
    async def large(event):
        return ToolContent('long result' * 2000)
    output, _ = await bot.tool_executor.execute(ctx, 'example__send', {}, registry)
    assert len(output) <= plugin.ai_tools[0].max_output_chars
    assert json.loads(output)['truncated']
    assert seen == ['first', 'last']


async def test_failed_action_notifies_observers_and_skips_result_rewrite(extension):
    bot, plugin, callback, ctx, registry, _ = extension
    callback.side_effect = RuntimeError('external action may have happened')
    observed = []
    @plugin.on_tool('after', name='rewrite')
    async def rewrite(event):
        pytest.fail('failed execution must not be rewritten')
    @plugin.on_tool('completed', name='observe')
    async def observe(event):
        observed.append(event)
    output, _ = await bot.tool_executor.execute(ctx, 'example__send', {}, registry)
    assert json.loads(output)['error'] == 'tool_uncertain'
    assert observed[0].status == 'uncertain'
    assert observed[0].execution_id
    assert observed[0].content == output


async def test_invalid_arguments_do_not_enter_hooks(extension):
    bot, plugin, callback, ctx, registry, _ = extension
    gate = AsyncMock()
    plugin.on_tool('before', name='gate')(gate)
    output, _ = await bot.tool_executor.execute(ctx, 'example__send', '{', registry)
    assert json.loads(output)['error'] == 'invalid_arguments'
    gate.assert_not_awaited()
    callback.assert_not_awaited()


async def test_expired_context_does_not_reach_result_hooks(extension):
    bot, plugin, callback, ctx, registry, _ = extension
    async def expire(*args, **kwargs):
        ctx.cancellation_reason = 'context_cleared'
        return {'done': True}
    callback.side_effect = expire
    observer = AsyncMock()
    plugin.on_tool('after', name='format')(observer)
    plugin.on_tool('completed', name='observe')(observer)
    await bot.tool_executor.execute(ctx, 'example__send', {}, registry)
    callback.assert_awaited_once()
    observer.assert_not_awaited()


async def test_cancellation_stops_before_hook_without_executing_tool(extension):
    bot, plugin, callback, ctx, registry, _ = extension
    entered = asyncio.Event()
    @plugin.on_tool('before', name='waiting')
    async def waiting(event):
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(bot.tool_executor.execute(ctx, 'example__send', {}, registry))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    callback.assert_not_awaited()


def test_registration_rejects_duplicate_names_and_invalid_hooks():
    plugin = Plugin('test')
    plugin.agent_instruction('policy', tools=['read'], text='policy')
    with pytest.raises(ValueError):
        plugin.agent_instruction('policy', tools=['read'], text='other')
    with pytest.raises(ValueError):
        plugin.on_tool('unknown', name='test')
    with pytest.raises(ValueError):
        plugin.on_tool('before', name='test', timeout=float('nan'))
    with pytest.raises(ValueError):
        plugin.on_tool('before', name='test')(lambda event: None)


async def test_dispatched_agent_request_contains_only_available_plugin_instructions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    directory = tmp_path / 'plugins'
    directory.mkdir()
    (directory / 'policy_demo.py').write_text('''from fllbot.plugin import Plugin
plugin = Plugin('policy_demo', enabled_by_default=True)
plugin.agent_instruction('workflow', tools=['read'], text='POLICY_DEMO_WORKFLOW')
@plugin.on_ai_tool('read', description='test', parameters={'type':'object'})
async def read(ctx):
    return {'done': True}
''', encoding='utf-8')
    bot = await make_bot(tmp_path, plugin_dirs=[directory], llm={
        'enabled': True, 'api_key': 'test', 'context_window': 16000})
    complete = AsyncMock(return_value=SimpleNamespace(text='answer', tool_calls=[]))
    monkeypatch.setattr(bot.chat, 'complete', complete)
    try:
        for index, names in enumerate([['policy_demo__read'], []]):
            await bot.permissions.save_group(20002, enabled=True, config={'policy': {
                'auto_group': True, 'trigger_modes': ['all'], 'cooldown_seconds': 0,
                'allowed_tools': names}})
            await bot.dispatcher.dispatch(parse_event(group_message('hello', message_id=index + 1)), FakeConnection())
        assert complete.await_count == 2
        first = complete.await_args_list[0].args[0][0]['content']
        second = complete.await_args_list[1].args[0][0]['content']
        assert '[插件策略 policy_demo__workflow]' in first
        assert 'POLICY_DEMO_WORKFLOW' in first
        assert 'POLICY_DEMO_WORKFLOW' not in second
    finally:
        await bot.stop()
