"""Batch persistence, revision conflicts and secret redaction use temporary stores."""

import asyncio

import httpx
import pytest

from fllbot.app import create_app
from fllbot.plugin.settings import MASK
from helpers import make_bot

AUTH = {'Authorization': 'Bearer admin-token'}


@pytest.fixture
async def settings_bot(tmp_path):
    directory = tmp_path/'plugins'
    directory.mkdir()
    (directory/'demo.py').write_text('''from fllbot.plugin import Plugin
plugin = Plugin('demo')
plugin.setting('token', secret=True, default='fake-default-secret')
plugin.setting('first', type='int', default=1)
plugin.setting('second', type='int', default=2, group_scoped=True)
''', encoding='utf-8')
    bot = await make_bot(tmp_path, plugin_dirs=[directory])
    try:
        yield bot, bot.plugins.get('demo').settings
    finally:
        await bot.stop()


async def test_plugin_revisions_and_secret_roundtrip(settings_bot):
    bot, settings = settings_bot
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
        current = await client.get('/api/plugins/demo/settings')
        revision = current.json()['revision']
        assert current.headers['etag'] == revision
        assert 'fake-default-secret' not in current.text
        assert current.json()['schema'][0]['default'] == MASK
        replies = await asyncio.gather(*[
            client.patch('/api/plugins/demo/settings', headers={'If-Match': revision}, json={'values': {'first': number, 'token': 'new-fake-secret'}})
            for number in (3, 4)
        ])
        assert sorted(response.status_code for response in replies) == [200, 409]
        saved = next(reply for reply in replies if reply.status_code == 200)
        assert 'new-fake-secret' not in saved.text
        assert saved.json()['effective']['token'] == MASK
        assert saved.headers['etag'] == saved.json()['revision'] != revision
        kept = await client.patch('/api/plugins/demo/settings', json={'values': {'token': MASK}})
        assert kept.status_code == 200 and settings.get('token') == 'new-fake-secret'
        assert kept.json()['revision'] == saved.json()['revision']
        cleared = await client.patch('/api/plugins/demo/settings', json={'values': {'token': ''}})
        assert cleared.status_code == 200 and settings.get('token') == ''
        audit = await (await bot.storage.db.execute("SELECT fields FROM management_changes WHERE action='plugin_settings_updated'")).fetchall()
        assert audit and 'new-fake-secret' not in str([row[0] for row in audit])


async def test_storage_failure_rolls_back_mixed_scope_batch(settings_bot):
    bot, settings = settings_bot
    await bot.storage.db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON kv WHEN NEW.scope='plugin-settings:demo' AND NEW.key='123:second' BEGIN SELECT RAISE(ABORT,'test second write failure'); END")
    await bot.storage.db.commit()
    before = settings.revision(123)
    with pytest.raises(Exception, match='second write failure'):
        await settings.update({'first': 7, 'second': 8}, 123)
    assert settings.get('first') == 1 and settings.get('second', group_id=123) == 2
    assert settings.revision(123) == before
    assert not await bot.storage.kv_all(settings.scope)
    records = await (await bot.storage.db.execute("SELECT count(*) FROM management_changes WHERE action='plugin_settings_updated'")).fetchone()
    assert records[0] == 0
    await bot.storage.db.execute('DROP TRIGGER fail_second')
    await bot.storage.db.commit()
    await settings.update({'first': 7, 'second': 8}, 123)
    assert settings.get('first') == 7 and settings.get('second', group_id=123) == 8
    assert await bot.storage.kv_all(settings.scope) == {'0:first': 7, '123:second': 8}
    rows = await (await bot.storage.db.execute("SELECT group_id FROM management_changes WHERE action='plugin_settings_updated' ORDER BY id")).fetchall()
    assert [row[0] for row in rows] == [0, 123]


async def test_cancellation_after_persistence_still_publishes_batch(settings_bot, monkeypatch):
    bot, settings = settings_bot
    entered, release = asyncio.Event(), asyncio.Event()
    original = bot.storage.save_plugin_settings

    async def delayed(*args, **kwargs):
        await original(*args, **kwargs)
        entered.set()
        await release.wait()

    monkeypatch.setattr(bot.storage, 'save_plugin_settings', delayed)
    task = asyncio.create_task(settings.update({'first': 8, 'second': 9}, 123))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert settings.get('first') == 8 and settings.get('second', group_id=123) == 9


async def test_agent_group_policy_changes_invalidate_plugin_revision(settings_bot):
    bot, _ = settings_bot
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
        old = (await client.get('/api/plugins/agent/settings?group_id=123')).json()['revision']
        await bot.permissions.save_group(123, enabled=True, config={'policy': {'auto_group': True}})
        response = await client.patch('/api/plugins/agent/settings', headers={'If-Match': old}, json={'group_id': 123, 'values': {'auto_group': False}})
        assert response.status_code == 409
        assert not bot.plugins.get('agent').settings.group_overrides(123)


async def test_group_scope_rejects_ambiguous_values(settings_bot):
    bot, settings = settings_bot
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
        for group_id in (True, 12.5, -1, 0, 'wrong'):
            response = await client.patch('/api/plugins/demo/settings', json={'group_id': group_id, 'values': {'second': 9}})
            assert response.status_code == 400
        assert settings.get('second') == 2 and not await bot.storage.kv_all(settings.scope)
