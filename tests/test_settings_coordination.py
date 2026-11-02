import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from fllbot.app import create_app
from fllbot.storage.locking import SettingsLock
from helpers import make_bot

AUTH = {"Authorization": "Bearer admin-token"}


async def test_console_and_legacy_group_api_share_revision_guard(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            old = (await client.get('/api/console/groups/123')).json()['revision']
            first, second = await asyncio.gather(
                client.patch('/api/console/groups/123', headers={'If-Match': old}, json={'enabled': True}),
                client.patch('/api/groups/123/config', headers={'If-Match': old}, json={'custom': 'preserve'}),
            )
            assert sorted([first.status_code, second.status_code]) == [200, 409]
            current = (await client.get('/api/groups/123/config')).json()
            assert current['revision'] != old
            rejected = await client.post('/api/groups/123/enabled', headers={'If-Match': old}, json={'enabled': False})
            assert rejected.status_code == 409
            # Legacy unconditional patches remain compatible and merge under the same lock.
            responses = await asyncio.gather(*[
                client.patch('/api/groups/123/config', json={key: key}) for key in ('left', 'right')
            ])
            assert all(response.status_code == 200 for response in responses)
            assert bot.permissions.group_config(123)['left'] == 'left'
            assert bot.permissions.group_config(123)['right'] == 'right'
    finally:
        await bot.stop()


async def test_global_config_revision_and_failed_persistence_do_not_publish(tmp_path: Path, monkeypatch) -> None:
    bot = await make_bot(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            original = (await client.get('/api/config')).json()['revision']
            responses = await asyncio.gather(*[
                client.patch('/api/config', headers={'If-Match': original}, json={'bot': {'error_reply': value}})
                for value in ('first', 'second')
            ])
            assert sorted(response.status_code for response in responses) == [200, 409]
        previous = bot.config.model_dump()
        overrides = bot.config_store.overrides
        with monkeypatch.context() as patcher:
            patcher.setattr(bot.storage, 'meta_set', AsyncMock(side_effect=OSError('simulated write failure')))
            with pytest.raises(OSError):
                await bot.update_config({'permission': {'default_group_enabled': False}})
        assert bot.config.model_dump() == previous
        assert bot.config_store.overrides == overrides
        # Returned group snapshots cannot mutate runtime state before a save.
        await bot.permissions.save_group(123, enabled=True, config={'llm': {'persona': 'old'}})
        copied = bot.permissions.group_config(123)
        copied['llm']['persona'] = 'new'
        assert bot.permissions.group_config(123)['llm']['persona'] == 'old'
    finally:
        await bot.stop()


async def test_settings_lock_excludes_child_tasks_and_recovers_after_cancellation() -> None:
    lock = SettingsLock()
    entered = asyncio.Event()

    async def writer():
        async with lock:
            entered.set()

    async with lock:
        async with lock:
            blocked = asyncio.create_task(writer())
            await asyncio.sleep(0)
            assert not entered.is_set()
            blocked.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked
        another = asyncio.create_task(writer())
        await asyncio.sleep(0)
        assert not entered.is_set()
    await asyncio.wait_for(another, timeout=2)
    assert entered.is_set()


async def test_plugin_mutations_wait_for_management_guard(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path)
    try:
        agent = bot.plugins.get('agent')
        original = bot.permissions.group_revision(123)
        async with bot.storage.settings_lock:
            pending = asyncio.create_task(agent.settings.set('auto_group', True, group_id=123))
            await asyncio.sleep(0)
            assert not pending.done()
            assert bot.permissions.group_revision(123) == original
        await asyncio.wait_for(pending, timeout=2)
        assert bot.permissions.group_revision(123) != original
    finally:
        await bot.stop()


async def test_chat_editor_revision_is_in_headers_and_detects_other_config_writes(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            response = await client.get('/api/console/chat')
            old = response.headers['etag']
            values = response.json()
            assert 'revision' not in values
            await client.patch('/api/config', json={'chat': {'wake_word': 'new'}})
            assert (await client.put('/api/console/chat', headers={'If-Match': old}, json=values)).status_code == 409
            assert bot.config.chat.wake_word == 'new'
            current = await client.get('/api/console/chat')
            saved = await client.put('/api/console/chat', headers={'If-Match': current.headers['etag']}, json=current.json())
            assert saved.status_code == 200
            assert saved.headers['etag'] == current.headers['etag']
    finally:
        await bot.stop()
