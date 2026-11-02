import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from fllbot.app import create_app
from fllbot.maintenance import BackupBusyError
from fllbot.plugin import Plugin
from fllbot.storage import SCHEMA_VERSION, Storage
from fllbot.storage.backup import DatabaseInUseError, restore_backup, validate_database
from helpers import make_bot

AUTH = {'Authorization': 'Bearer admin-token'}


async def test_backup_job_list_validate_download_and_offline_restore(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path)
    await bot.permissions.save_group(123, enabled=True, config={'policy': {'auto_group': True}})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            assert (await client.get('/api/console/backups')).status_code == 401
            assert (await client.post('/api/console/backups')).status_code == 401
            started = await client.post('/api/console/backups', headers=AUTH)
            assert started.status_code == 202
            job_id = started.json()['id']
            await bot.backups.task
            job = (await client.get('/api/console/backup-jobs/'+job_id, headers=AUTH)).json()
            assert job['status'] == 'completed'
            name = job['result']['name']
            listing = (await client.get('/api/console/backups', headers=AUTH)).json()
            assert listing['scope'] == 'database'
            assert listing['items'][0]['name'] == name
            assert listing['items'][0]['validation'] == 'valid'
            verified = await client.post('/api/console/backups/'+name+'/validate', headers=AUTH)
            assert verified.json()['schema_version'] == SCHEMA_VERSION
            downloaded = await client.get('/api/console/backups/'+name+'/download', headers=AUTH)
            assert downloaded.status_code == 200
            assert downloaded.content.startswith(b'SQLite format 3')
            assert downloaded.headers['cache-control'] == 'no-store'
            assert (await client.get('/api/console/backups/'+name+'/download')).status_code == 401
            guide = (await client.get('/api/console/backups/'+name+'/restore-guide', headers=AUTH)).json()
            assert guide['requires_shutdown'] is True
            assert ' restore ' in guide['command'] and '--confirm' in guide['command']
            source = bot.backups.file(name)
            with pytest.raises(DatabaseInUseError):
                restore_backup(source, bot.storage.path, max_schema_version=SCHEMA_VERSION)
            await bot.permissions.set_group_enabled(123, False)
            # Existing synchronous API stays compatible and shares backup management.
            old_api = await client.post('/api/storage/backup', headers=AUTH)
            assert old_api.status_code == 200 and old_api.json()['ok'] is True
            first = (await client.get('/api/console/backups?limit=1', headers=AUTH)).json()
            second = (await client.get('/api/console/backups?limit=1&before='+first['next_cursor'], headers=AUTH)).json()
            assert len(first['items']) == len(second['items']) == 1
            assert first['items'][0]['name'] != second['items'][0]['name']
    finally:
        await bot.stop()
    metadata, safety = restore_backup(source, bot.storage.path, max_schema_version=SCHEMA_VERSION)
    assert metadata['schema_version'] == SCHEMA_VERSION
    assert safety and validate_database(safety, max_schema_version=SCHEMA_VERSION)
    with sqlite3.connect(safety) as connection:
        assert connection.execute('SELECT enabled FROM groups WHERE group_id=123').fetchone()[0] == 0
    restored = Storage(bot.storage.path)
    await restored.connect()
    try:
        assert (await restored.list_group_states())[123]['enabled'] is True
    finally:
        await restored.close()


async def test_backup_paths_and_validation_do_not_expose_other_files(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path)
    try:
        directory = bot.backups.directory
        directory.mkdir(parents=True, exist_ok=True)
        invalid = directory/'fllbot-20261102-120000.db'
        invalid.write_bytes(b'not a database')
        (directory/'unrelated.db').write_bytes(b'PRIVATE')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            assert (await client.post('/api/console/backups/'+invalid.name+'/validate')).status_code == 400
            assert (await client.get('/api/console/backups/unrelated.db/download')).status_code == 400
            assert (await client.get('/api/console/backups?before=../private.db')).status_code == 400
            assert (await client.get('/api/console/backups/fllbot-20261102-000000.db/download')).status_code == 404
            assert (await client.get('/api/console/backup-jobs/'+'0'*32)).status_code == 404
            names = [item['name'] for item in (await client.get('/api/console/backups')).json()['items']]
            assert names == [invalid.name]
        escaping = directory/'fllbot-20261102-120001.db'
        try:
            escaping.symlink_to(tmp_path/'data'/'fllbot.db')
        except OSError:
            pass  # Windows may not grant symbolic-link creation to the test process.
        else:
            with pytest.raises(ValueError):
                bot.backups.file(escaping.name)
            assert escaping.name not in [item['name'] for item in bot.backups.index()['items']]
    finally:
        await bot.stop()


async def test_backup_conflicts_failures_and_request_cancellation_do_not_restart_job(tmp_path: Path, monkeypatch) -> None:
    bot = await make_bot(tmp_path)
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def work():
        entered.set()
        await gate.wait()
        raise OSError('PRIVATE write error')

    monkeypatch.setattr(bot.backups, '_create', work)
    try:
        waiting = asyncio.create_task(bot.backups.create_now())
        await entered.wait()
        with pytest.raises(BackupBusyError):
            bot.backups.start()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert bot.backups.active is True
        assert not bot.backups.task.done()
        gate.set()
        await bot.backups.task
        assert bot.backups.active is False
        job = next(iter(bot.backups.jobs.values()))
        assert job['status'] == 'failed'
        assert 'PRIVATE' not in json.dumps(job)
    finally:
        gate.set()
        await bot.stop()


async def test_service_get_is_cache_only_and_results_are_normalized(tmp_path: Path, monkeypatch) -> None:
    bot = await make_bot(tmp_path)
    plugin = Plugin(name='demo')
    calls = []

    @plugin.service_check('health', 'Demo')
    async def health():
        calls.append('check')
        return {'state': 'idle', 'code': 'idle', 'error': 'PRIVATE server body'}

    monkeypatch.setattr(bot.plugins, 'loaded', lambda: [plugin])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            assert (await client.get('/api/console/services')).status_code == 401
            assert (await client.post('/api/console/services/check')).status_code == 401
            first = (await client.get('/api/console/services', headers=AUTH)).json()
            assert first['items'][0]['state'] == 'unknown'
            assert calls == []
            result = await client.post('/api/console/services/check', headers=AUTH, json={'service_id': 'demo__health'})
            assert result.json()['items'][0]['state'] == 'idle'
            assert 'PRIVATE' not in result.text
            assert calls == ['check']
            await client.get('/api/console/services', headers=AUTH)
            assert calls == ['check']
            assert (await client.post('/api/console/services/check', headers=AUTH, json={'service_id': 'missing'})).status_code == 404
            # A newly loaded plugin object must never inherit an old check result.
            replacement = Plugin(name='demo')
            replacement.service_check('health', 'New')(health)
            monkeypatch.setattr(bot.plugins, 'loaded', lambda: [replacement])
            assert (await client.get('/api/console/services', headers=AUTH)).json()['items'][0]['state'] == 'unknown'
    finally:
        monkeypatch.undo()
        await bot.stop()


async def test_service_timeout_and_parallel_check_conflict(tmp_path: Path, monkeypatch) -> None:
    bot = await make_bot(tmp_path)
    plugin = Plugin(name='demo')
    entered = asyncio.Event()

    @plugin.service_check('slow', 'Slow', timeout=0.02)
    async def slow():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(bot.plugins, 'loaded', lambda: [plugin])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            first = asyncio.create_task(client.post('/api/console/services/check'))
            await entered.wait()
            assert (await client.post('/api/console/services/check')).status_code == 409
            result = (await first).json()['items'][0]
            assert result['state'] == 'unavailable' and result['code'] == 'timeout'
            assert bot.services.busy is False
    finally:
        monkeypatch.undo()
        await bot.stop()


@pytest.mark.parametrize(('payload', 'state'), [({'ok': True}, 'ready'), ({'ok': False, 'state': 'idle'}, 'idle'),
                                                   ({'ok': False, 'state': 'loading'}, 'loading'), ({'ok': False, 'error': 'PRIVATE'}, 'error')])
async def test_voice_checks_only_use_health_get(payload, state, monkeypatch) -> None:
    path = Path(__file__).resolve().parents[1]/'plugins'/'voice'/'plugin.py'
    spec = importlib.util.spec_from_file_location('voice_health_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append((request.method, str(request.url)))
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    result = await module.check_qwen()
    assert result['state'] == state
    assert requests == [('GET', 'http://127.0.0.1:7862/health')]
    assert 'PRIVATE' not in json.dumps(result)
