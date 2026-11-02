"""Startup config, management isolation and migration use isolated instances."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.config import STARTUP_FIELDS, StartupConfigError, deep_merge
from fllbot.storage import Storage
from helpers import build_bot, make_bot

AUTH = {'Authorization': 'Bearer admin-token'}


async def test_all_startup_fields_reject_mixed_patch_without_persistence(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        original = bot.config.model_dump()
        revision = bot.config_revision()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            for path in sorted(STARTUP_FIELDS):
                section, key = path.split('.')
                value = original[section][key]
                changed = not value if isinstance(value, bool) else value+1 if isinstance(value, (float, int)) else ['different'] if isinstance(value, list) else 'different'
                patch = {section: {key: changed}}
                patch = deep_merge(patch, {'permission': {'allow_private': False}})
                response = await client.patch('/api/config', json=patch)
                assert response.status_code == 409, (path, response.text)
                assert response.json()['detail']['code'] == 'startup_config_read_only'
                assert path in response.json()['detail']['fields']
                assert bot.config.model_dump() == original
                assert bot.config_revision() == revision
                assert await bot.storage.meta_get('config_overrides') is None
            # No-op startup values are accepted but never written as runtime overrides.
            response = await client.patch('/api/config', json={'server': {'port': str(original['server']['port'])}, 'permission': {'allow_private': False}})
            assert response.status_code == 200
            assert bot.config.permission.allow_private is False
            assert 'port' not in bot.config_store.overrides.get('server', {})
            await bot.update_config({'server': {'heartbeat_timeout': 2}, 'storage': {'backup_dir': 'other-backups'}})
            assert bot.manager.heartbeat_timeout == 2
            assert bot.config.storage.backup_dir == 'other-backups'
            with pytest.raises(StartupConfigError):
                bot.config_store.patch({'server': {'port': 1}})
    finally:
        await bot.stop()


async def test_legacy_startup_overrides_are_retained_once_not_applied(tmp_path):
    initial = build_bot(tmp_path)
    original_port = initial.config.server.port
    legacy = deep_merge(initial.config_store.overrides, {'server': {'port': original_port+1},
                        'storage': {'path': 'never-open-this.db'}, 'admin': {'enabled': False},
                        'plugin': {'max_concurrency': 1, 'dirs': ['never-load-this']},
                        'permission': {'allow_private': False}})
    store = Storage(initial.storage.path)
    await store.connect()
    await store.meta_set('config_overrides', legacy)
    await store.close()
    bot = await make_bot(tmp_path)
    try:
        assert bot.config.server.port == original_port
        assert bot.config.storage.path == str(initial.storage.path)
        assert bot.config.admin.enabled is True
        assert bot.config.plugin.max_concurrency == bot.dispatcher._worker_count == 8
        assert bot.inactive_startup_overrides['plugin.max_concurrency'] == 1
        assert 'never-load-this' not in bot.config.plugin.dirs
        assert bot.config.permission.allow_private is False
        assert not (tmp_path/'never-open-this.db').exists()
        saved = await bot.storage.meta_get('config_overrides')
        assert 'port' not in saved['server'] and 'path' not in saved.get('storage', {})
        assert bot.inactive_startup_overrides['server.port'] == original_port+1
        assert bot.inactive_startup_overrides['admin.enabled'] is False
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            response = await client.get('/api/config')
            assert response.status_code == 200
            assert response.json()['startup']['active']['server.port'] == original_port
            assert response.json()['startup']['inactive_overrides']['storage.path'] == 'never-open-this.db'
            assert 'admin-token' not in response.text and 'test-token' not in response.text
        first = await (await bot.storage.db.execute("SELECT count(*) FROM management_changes WHERE action='startup_overrides_archived'")).fetchone()
        assert first[0] == 1
    finally:
        await bot.stop()
    restarted = await make_bot(tmp_path)
    try:
        assert restarted.config.server.port == original_port
        assert restarted.inactive_startup_overrides['server.port'] == original_port+1
        second = await (await restarted.storage.db.execute("SELECT count(*) FROM management_changes WHERE action='startup_overrides_archived'")).fetchone()
        assert second[0] == 1
    finally:
        await restarted.stop()


async def test_admin_disabled_closes_all_management_surfaces(tmp_path):
    bot = await make_bot(tmp_path, overrides={'admin': {'enabled': False}})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            for path in ['/api/status', '/api/config', '/api/console/bots', '/api/console/task-sources',
                         '/ui', '/api/docs', '/api/openapi.json', '/api/events/stream?token=admin-token']:
                response = await asyncio.wait_for(client.get(path), 1)
                assert response.status_code == 404, path
            assert (await client.get('/api/health')).status_code == 200
            assert (await client.get('/api/version')).status_code == 200
            assert (await client.patch('/api/config', json={'admin': {'enabled': True}})).status_code == 404
            assert (await client.post('/api/console/backups')).status_code == 404
    finally:
        await bot.stop()


def test_admin_disabled_preserves_onebot_connection(tmp_path):
    bot = build_bot(tmp_path, overrides={'admin': {'enabled': False}})
    bot.bots.sync = AsyncMock()
    with TestClient(create_app(bot)) as client:
        with client.websocket_connect('/onebot/v11/ws?access_token=test-token', headers={'x-self-id': '10001'}):
            assert client.get('/api/health').json()['connections'] == 1
            assert client.get('/api/status', headers=AUTH).status_code == 404


async def test_configuration_errors_never_echo_persisted_secret(tmp_path):
    secret = 'persisted-sensitive-key-for-test'
    bot = await make_bot(tmp_path, llm={'api_key': secret})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            response = await client.patch('/api/config', json={'llm': {'fallback_providers': ['missing']}})
            assert response.status_code == 400
            assert secret not in response.text and 'input_value' not in response.text
            assert 'llm' in response.text
        assert bot.config.llm.api_key == secret
    finally:
        await bot.stop()


async def test_cancelled_global_config_commit_still_publishes(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path)
    original = bot.storage.meta_set
    committed, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        await original(*args, **kwargs)
        committed.set()
        await release.wait()

    monkeypatch.setattr(bot.storage, 'meta_set', delayed)
    task = asyncio.create_task(bot.update_config({'permission': {'allow_private': False}}))
    try:
        await asyncio.wait_for(committed.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        assert bot.config.permission.allow_private is False
        assert (await bot.storage.meta_get('config_overrides'))['permission']['allow_private'] is False
    finally:
        await bot.stop()


async def test_startup_archive_failure_rolls_back_original_overrides(tmp_path):
    storage = Storage(tmp_path/'archive.db')
    await storage.connect()
    original = {'server': {'port': 9000}, 'permission': {'allow_private': False}}
    try:
        await storage.meta_set('config_overrides', original)
        await storage.db.execute("CREATE TRIGGER fail_archive BEFORE INSERT ON meta WHEN NEW.key='inactive_startup_overrides' BEGIN SELECT RAISE(ABORT,'archive rollback'); END")
        await storage.db.commit()
        with pytest.raises(Exception, match='archive rollback'):
            await storage.archive_startup_overrides({'permission': {'allow_private': False}}, {'server.port': 9000})
        assert await storage.meta_get('config_overrides') == original
        assert await storage.meta_get('inactive_startup_overrides') is None
        rows = await (await storage.db.execute("SELECT count(*) FROM management_changes WHERE action='startup_overrides_archived'")).fetchone()
        assert rows[0] == 0
    finally:
        await storage.close()


async def test_failed_restore_releases_database_before_aborting_startup(tmp_path, monkeypatch):
    bot = build_bot(tmp_path)
    seed = Storage(bot.storage.path)
    await seed.connect()
    await seed.meta_set('config_overrides', {'server': {'port': 9999}})
    await seed.close()
    monkeypatch.setattr(bot.storage, 'archive_startup_overrides', AsyncMock(side_effect=OSError('archive failed')))
    with pytest.raises(OSError, match='archive failed'):
        await bot.start()
    assert not bot.started
    again = Storage(bot.storage.path)
    await again.connect()
    try:
        assert await again.meta_get('config_overrides') == {'server': {'port': 9999}}
    finally:
        await again.close()
