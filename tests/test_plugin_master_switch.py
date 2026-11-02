"""An explicit global stop overrides group opt-ins without deleting them."""

import asyncio
import json
import sys

import httpx
import pytest

from fllbot.app import create_app
from fllbot.builtin_plugins.agent.plugin import _run_tool
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot

AUTH = {'Authorization': 'Bearer admin-token'}
URL = '/api/plugins/gated/enabled'
SOURCE = '''import asyncio
from fllbot.plugin import Plugin
plugin=Plugin('gated', enabled_by_default=False)
seen=asyncio.Event()
ticks=0
tool_calls=0
@plugin.on_message()
async def message(ctx):
    await ctx.reply('gated reply')
@plugin.on_ai_tool('ping',description='test',parameters={'type':'object','properties':{},'additionalProperties':False})
async def ping(ctx):
    global tool_calls
    tool_calls+=1
    return {'called': True}
@plugin.on_loop(seconds=0.01)
async def tick(bot):
    global ticks
    ticks+=1
    seen.set()
'''


@pytest.fixture
async def gated_bot(tmp_path):
    directory = tmp_path/'plugins'
    directory.mkdir()
    (directory/'gated.py').write_text(SOURCE, encoding='utf-8')
    bot = await make_bot(tmp_path, plugin_dirs=[directory])
    owner = bot.plugins.get('gated')
    module = sys.modules[owner.ai_tools[0].callback.__module__]
    try:
        yield bot, owner, module
    finally:
        await bot.stop()


async def test_default_optin_global_stop_blocks_messages_tools_and_loops(gated_bot):
    bot, owner, module = gated_bot
    conn = FakeConnection()
    event = parse_event(group_message('hello', group_id=123))
    ctx = Context(bot, conn, event)
    registry = {'gated__ping': (owner, owner.ai_tools[0])}
    assert not bot.permissions.plugin_enabled('gated', 123)
    assert not bot.permissions.plugin_loop_enabled('gated')
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(module.seen.wait(), 0.04)
    await bot.permissions.set_plugin_enabled('gated', True, 123)
    await asyncio.wait_for(module.seen.wait(), 1)
    await bot.dispatcher.dispatch(event, conn)
    assert len(conn.sent) == 1
    assert json.loads((await _run_tool(ctx, 'gated__ping', {}, registry))[0]) == {'called': True}
    assert module.tool_calls == 1
    await bot.permissions.set_plugin_enabled('gated', False)
    assert bot.permissions.plugin_state('gated', 123) is True
    assert not bot.permissions.plugin_enabled('gated', 123)
    assert not bot.permissions.plugin_loop_enabled('gated')
    module.seen.clear()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(module.seen.wait(), 0.04)
    await bot.dispatcher.dispatch(parse_event(group_message('again', group_id=123, message_id=2)), conn)
    await _run_tool(ctx, 'gated__ping', {}, registry)
    assert len(conn.sent) == 1 and module.tool_calls == 1
    await bot.permissions.set_plugin_enabled('gated', True)
    await asyncio.wait_for(module.seen.wait(), 1)
    await bot.dispatcher.dispatch(parse_event(group_message('restored', group_id=123, message_id=3)), conn)
    assert len(conn.sent) == 2
    await bot.permissions.set_plugin_enabled('gated', False, 123)
    assert not bot.permissions.plugin_enabled('gated', 123)
    assert bot.permissions.plugin_enabled('gated', 456)


async def test_switch_status_versions_conflicts_and_group_state_retention(gated_bot):
    bot, _, _ = gated_bot
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
        assert (await client.get(URL)).status_code == 401
        assert (await client.post(URL, json={'enabled': True})).status_code == 401
        initial = await client.get(URL, headers=AUTH)
        assert initial.headers['etag'] == initial.json()['revision']
        old = initial.json()['revision']
        replies = await asyncio.gather(*[
            client.post(URL, headers={**AUTH, 'If-Match': old}, json={'enabled': value}) for value in [True, False]
        ])
        assert sorted(reply.status_code for reply in replies) == [200, 409]
        await client.post(URL, headers=AUTH, json={'enabled': True, 'group_id': 123})
        group = (await client.get(URL+'?group_id=123', headers=AUTH)).json()
        await client.post(URL, headers=AUTH, json={'enabled': False})
        stopped = (await client.get(URL+'?group_id=123', headers=AUTH)).json()
        assert stopped['group_state'] is True and not stopped['enabled']
        assert stopped['blocked_by_global'] and stopped['source'] == 'global'
        assert (await client.post(URL, headers={**AUTH, 'If-Match': group['revision']}, json={'enabled': False, 'group_id': 123})).status_code == 409
        set_while_stopped = await client.post(URL, headers=AUTH, json={'enabled': True, 'group_id': 456})
        assert set_while_stopped.json()['enabled'] is True
        assert set_while_stopped.json()['effective']['enabled'] is False
        assert (await client.post(URL, headers=AUTH, json={'group_id': 0})).status_code == 422
        assert (await client.get('/api/plugins/missing/enabled', headers=AUTH)).status_code == 404
        entries = (await client.get('/api/plugins', headers=AUTH)).json()
        entry = next(item for item in entries if item['name'] == 'gated')
        assert entry['blocked_by_global'] and entry['revision']
        audits = await (await bot.storage.db.execute("SELECT fields FROM management_changes WHERE action='plugin_state_updated'")).fetchall()
        assert audits and all('plugin.gated.enabled' in row[0] for row in audits)


async def test_master_switch_persists_and_audit_failure_rolls_back(gated_bot, tmp_path):
    bot, _, _ = gated_bot
    await bot.permissions.set_plugin_enabled('gated', True, 123)
    await bot.permissions.set_plugin_enabled('gated', False)
    previous = bot.permissions.plugin_revision('gated')
    await bot.storage.db.execute("CREATE TRIGGER fail_plugin_state BEFORE INSERT ON management_changes WHEN NEW.action='plugin_state_updated' BEGIN SELECT RAISE(ABORT,'plugin rollback'); END")
    await bot.storage.db.commit()
    with pytest.raises(Exception, match='plugin rollback'):
        await bot.permissions.set_plugin_enabled('gated', True)
    assert not bot.permissions.plugin_enabled('gated', 123)
    assert bot.permissions.plugin_revision('gated') == previous
    assert (await bot.storage.list_plugin_states())[('gated', 0)] is False
    await bot.stop()
    restarted = await make_bot(tmp_path, plugin_dirs=[tmp_path/'plugins'])
    try:
        status = restarted.permissions.plugin_status('gated', 123)
        assert not status['enabled'] and status['group_state'] is True
        assert status['blocked_by_global']
    finally:
        await restarted.stop()


async def test_cancelled_switch_publishes_committed_state(gated_bot, monkeypatch):
    bot, _, _ = gated_bot
    original = bot.storage.set_plugin_state
    committed, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        await original(*args, **kwargs)
        committed.set()
        await release.wait()

    monkeypatch.setattr(bot.storage, 'set_plugin_state', delayed)
    save = asyncio.create_task(bot.permissions.set_plugin_enabled('gated', False))
    try:
        await asyncio.wait_for(committed.wait(), 2)
        save.cancel()
        await asyncio.sleep(0)
        assert not save.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await save
    assert bot.permissions.plugin_state('gated') is False
    assert (await bot.storage.list_plugin_states())[('gated', 0)] is False
