"""Task adapters, delivery receipts and recovery use temporary plugin databases."""

import asyncio
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from fllbot.app import create_app
from fllbot.plugin import Plugin
from helpers import FakeConnection, make_bot

AUTH = {'Authorization': 'Bearer admin-token'}
SOURCE = '/api/console/tasks/daily__reminders'


@pytest.fixture
async def task_bot(tmp_path):
    plugins = tmp_path / 'plugins'
    shutil.copytree(Path(__file__).resolve().parents[1] / 'plugins/daily', plugins / 'daily')
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[plugins])
    plugin = bot.plugins.get('daily')
    module = sys.modules[plugin.task_sources[0].callback.__module__]
    try:
        yield bot, module
    finally:
        await bot.stop()


async def create(module, *, self_id=10001, group=123, owner=300, content='synthetic task', due=False):
    context = SimpleNamespace(data_dir=module.plugin.data_dir, self_id=self_id, user_id=owner,
                              group_id=group, is_group=group is not None)
    result = await module.create_task(context, content)
    if due:
        with module.database(module.plugin.data_dir) as db:
            db.execute('UPDATE tasks SET due=1 WHERE id=?', (result['id'],))
    return result['id']


async def test_task_sources_auth_filters_paging_and_versioned_actions(task_bot):
    bot, module = task_bot
    first = await create(module, due=True)
    second = await create(module, group=None, owner=301)
    await create(module, self_id=10002, group=124)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
        assert (await client.get('/api/console/task-sources')).status_code == 401
        assert (await client.post(SOURCE+f'/{first}/actions', json={'action': 'cancel', 'revision': '1'})).status_code == 401
        assert (await client.get('/api/console/task-sources', headers=AUTH)).json()['items'][0]['id'] == 'daily__reminders'
        items = (await client.get(SOURCE+'?self_id=10001&scene=private&owner=301', headers=AUTH)).json()['items']
        assert [item['id'] for item in items] == [second]
        assert items[0]['scene_id'] == 301 and items[0]['timezone'] == 'UTC+08:00'
        page = (await client.get(SOURCE+'?limit=1', headers=AUTH)).json()
        next_page = (await client.get(SOURCE+'?limit=1&before='+str(page['next_cursor']), headers=AUTH)).json()
        assert next_page['items'][0]['id'] == second
        assert (await client.get(SOURCE+'?scene_id=123', headers=AUTH)).status_code == 422
        row = (await client.get(SOURCE+'?scene=group&scene_id=123', headers=AUTH)).json()['items'][0]
        assert row['id'] == first
        changed = await client.post(SOURCE+f'/{first}/actions', headers=AUTH, json={'action': 'pause', 'revision': row['revision']})
        assert changed.json()['state'] == 'paused'
        assert (await client.post(SOURCE+f'/{first}/actions', headers=AUTH, json={'action': 'cancel', 'revision': row['revision']})).status_code == 409
        resumed = (await client.post(SOURCE+f'/{first}/actions', headers=AUTH, json={'action': 'resume', 'revision': changed.json()['revision']})).json()
        assert resumed['state'] == 'pending'
        cancelled = await client.post(SOURCE+f'/{first}/actions', headers=AUTH, json={'action': 'cancel', 'revision': resumed['revision']})
        assert cancelled.json()['state'] == 'cancelled'
        assert (await client.post(SOURCE+f'/{first}/actions', headers=AUTH, json={'action': 'retry', 'revision': cancelled.json()['revision'], 'acknowledge_duplicate': True})).status_code == 409
        runs = (await client.get(SOURCE+f'/{first}/runs?limit=1', headers=AUTH)).json()
        assert runs['items'][0]['code'] == 'cancel' and runs['next_cursor']
        assert 'synthetic task' not in str(runs)
        assert (await client.get(SOURCE+'/99999/runs', headers=AUTH)).status_code == 404
        await bot.plugins.reload('daily')
        persisted = (await client.get(SOURCE+'?state=cancelled', headers=AUTH)).json()['items']
        assert [item['id'] for item in persisted] == [first]
        assert (await client.get(SOURCE+f'/{first}/runs', headers=AUTH)).json()['items'][0]['code'] == 'cancel'
        await bot.plugins.unload('daily')
        assert (await client.get(SOURCE, headers=AUTH)).status_code == 404


async def test_uncertain_receipt_requires_ack_and_checks_permissions_again(task_bot):
    bot, module = task_bot
    task_id = await create(module, due=True)
    conn = FakeConnection(10001)
    conn.send_msg = AsyncMock(side_effect=TimeoutError('no receipt'))
    bot.manager.add(conn)
    await module.deliver_reminders(bot)
    await module.deliver_reminders(bot)
    assert conn.send_msg.await_count == 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
        row = (await client.get(SOURCE)).json()['items'][0]
        assert row['delivery'] == 'uncertain'
        payload = {'action': 'retry', 'revision': row['revision']}
        assert (await client.post(SOURCE+f'/{task_id}/actions', json=payload)).status_code == 409
        retried = await client.post(SOURCE+f'/{task_id}/actions', json={**payload, 'acknowledge_duplicate': True})
        assert retried.json()['delivery'] == 'waiting'
        assert conn.send_msg.await_count == 1  # Scheduling retry never sends immediately.
        await bot.permissions.set_group_enabled(123, False)
        await module.deliver_reminders(bot)
        assert conn.send_msg.await_count == 1
        blocked = (await client.get(SOURCE)).json()['items'][0]
        assert blocked['last_code'] == 'group_denied'
        with module.database(module.plugin.data_dir) as db:
            db.execute('UPDATE tasks SET retry_at=0')
        await bot.permissions.set_group_enabled(123, True)
        conn.send_msg.side_effect = None
        await module.deliver_reminders(bot)
        await module.deliver_reminders(bot)
        assert conn.send_msg.await_count == 2
        assert (await client.get(SOURCE)).json()['items'][0]['delivery'] == 'sent'


async def test_interrupted_send_is_visible_and_does_not_resend(task_bot):
    bot, module = task_bot
    task_id = await create(module, due=True)
    entered = asyncio.Event()

    async def wait_for_receipt(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    conn = FakeConnection(10001)
    conn.send_msg = AsyncMock(side_effect=wait_for_receipt)
    bot.manager.add(conn)
    delivery = asyncio.create_task(module.deliver_reminders(bot))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        row = (await module.task_console('list'))['items'][0]
        assert row['delivery'] == 'sending' and not row['actions']
        with pytest.raises(ValueError):
            await module.task_console('action', task_id=task_id, action='cancel', revision=row['revision'])
    finally:
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery
    row = (await module.task_console('list'))['items'][0]
    assert row['delivery'] == 'uncertain' and row['last_code'] == 'interrupted'
    await module.deliver_reminders(bot)
    assert conn.send_msg.await_count == 1
    assert (await module.task_console('runs', task_id=task_id))['items'][0]['status'] == 'uncertain'
    # Simulate a crash leaving the journal in sending, then load a fresh plugin instance.
    with module.database(module.plugin.data_dir) as db:
        db.execute("UPDATE tasks SET delivery='sending' WHERE id=?", (task_id,))
        db.execute("UPDATE task_runs SET status='sending',finished_at=NULL WHERE task_id=?", (task_id,))
    await bot.plugins.reload('daily')
    callback = bot.plugins.get('daily').task_sources[0].callback
    assert (await callback('list'))['items'][0]['delivery'] == 'uncertain'
    assert (await callback('runs', task_id=task_id))['items'][0]['code'] == 'interrupted'


def test_legacy_plugin_database_migrates_without_repeating_sent_tasks(tmp_path):
    from plugins.daily.store import database

    with sqlite3.connect(tmp_path/'daily.sqlite3') as db:
        db.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY,bot INTEGER,scene INTEGER,owner INTEGER,content TEXT,due REAL,state TEXT DEFAULT 'pending',notified INTEGER DEFAULT 0,retry_at REAL DEFAULT 0)")
        db.execute("INSERT INTO tasks(bot,scene,owner,content,due,notified) VALUES(1,2,3,'old',1,1)")
    with database(tmp_path) as db:
        row = db.execute('SELECT * FROM tasks').fetchone()
        assert row['notified'] == 1 and row['delivery'] == 'sent'
        assert db.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0


def test_task_sources_validate_names_duplicates_and_async_callbacks():
    plugin = Plugin('test')
    with pytest.raises(ValueError):
        plugin.task_source('bad/name', 'bad')
    with pytest.raises(ValueError):
        plugin.task_source('sync', 'sync')(lambda: {})

    @plugin.task_source('jobs', 'jobs')
    async def jobs(operation, **values):
        return {}

    with pytest.raises(ValueError):
        plugin.task_source('jobs', 'duplicate')
