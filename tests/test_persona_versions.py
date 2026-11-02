"""Shared persona edits use loaded-file revisions and atomic replacement."""

import asyncio
from pathlib import Path

import httpx
import pytest

from fllbot.app import create_app
from fllbot.llm.personas import PersonaStore
from helpers import make_bot, seed_personas

AUTH = {'Authorization': 'Bearer admin-token'}


async def test_stale_edits_deletes_and_duplicate_creates_are_rejected(tmp_path):
    seed_personas(tmp_path, 'friendly')
    bot = await make_bot(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            first = await client.get('/api/personas/friendly')
            revision = first.json()['revision']
            assert first.headers['etag'] == revision
            replies = await asyncio.gather(*[
                client.put('/api/personas/friendly', headers={'If-Match': revision}, json={'content': text})
                for text in ['first draft', 'second draft']
            ])
            assert sorted(reply.status_code for reply in replies) == [200, 409]
            saved = next(reply for reply in replies if reply.status_code == 200)
            assert saved.headers['etag'] != revision
            assert (await client.delete('/api/personas/friendly', headers={'If-Match': revision})).status_code == 409
            assert (await client.get('/api/personas/friendly')).json()['content'] == saved.json()['content']
            assert (await client.put('/api/personas/friendly', headers={'If-None-Match': '*'}, json={'content': 'duplicate'})).status_code == 409
            created = await client.put('/api/personas/new', headers={'If-None-Match': '*'}, json={'content': 'new'})
            assert created.status_code == 200
            assert (await client.delete('/api/personas/new', headers={'If-Match': created.json()['revision']})).status_code == 200
            assert (await client.put('/api/personas/new', headers={'If-Match': created.json()['revision']}, json={'content': 'resurrect'})).status_code == 409
            assert (await client.put('/api/personas/new', headers={'If-None-Match': 'invalid'}, json={'content': 'x'})).status_code == 400
            assert (await client.put('/api/personas/new', headers={'If-None-Match': '*', 'If-Match': revision}, json={'content': 'x'})).status_code == 400
    finally:
        await bot.stop()


async def test_external_file_edit_and_deletion_conflict_then_reload(tmp_path):
    seed_personas(tmp_path, 'friendly')
    bot = await make_bot(tmp_path)
    try:
        path = tmp_path / 'personas' / 'friendly.md'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            old = (await client.get('/api/personas/friendly')).json()['revision']
            path.write_text('edited outside the console', encoding='utf-8')
            assert (await client.put('/api/personas/friendly', headers={'If-Match': old}, json={'content': 'stale'})).status_code == 409
            assert path.read_text(encoding='utf-8') == 'edited outside the console'
            refreshed = (await client.get('/api/personas/friendly')).json()
            assert refreshed['content'] == 'edited outside the console' and refreshed['revision'] != old
            path.unlink()
            assert (await client.put('/api/personas/friendly', headers={'If-Match': refreshed['revision']}, json={'content': 'stale'})).status_code == 409
            assert not path.exists()
            path.write_text('appeared after catalog loaded', encoding='utf-8')
            assert (await client.put('/api/personas/friendly', headers={'If-None-Match': '*'}, json={'content': 'new'})).status_code == 409
            assert (await client.get('/api/personas')).json()[0]['name'] == 'friendly'
    finally:
        await bot.stop()


async def test_persona_audit_and_failed_default_delete_restores_reference(tmp_path, monkeypatch):
    seed_personas(tmp_path, 'friendly')
    bot = await make_bot(tmp_path)
    try:
        await bot.update_config({'llm': {'persona': 'friendly'}})
        path = tmp_path / 'personas' / 'friendly.md'

        def fail_delete(name):
            raise OSError('simulated filesystem failure')

        monkeypatch.setattr(bot.personas, 'delete', fail_delete)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot), raise_app_exceptions=False), base_url='http://test', headers=AUTH) as client:
            persona = (await client.get('/api/personas/friendly')).json()
            deleted = await client.delete('/api/personas/friendly', headers={'If-Match': persona['revision']})
            assert deleted.status_code == 500
            assert bot.config.llm.persona == 'friendly'
            assert path.exists()
            changes = await bot.storage.group_changes(0, limit=20)
            actions = [item['action'] for item in changes['items']]
            assert 'persona_delete_started' in actions
            assert 'persona_delete_failed' in actions
            assert 'persona_default_restore_failed' not in actions
    finally:
        await bot.stop()


def test_txt_and_custom_filename_are_updated_in_place(tmp_path):
    path = tmp_path / 'custom.txt'
    path.write_text('---\nname: friendly\n---\nold text', encoding='utf-8')
    store = PersonaStore([tmp_path])
    store.load()
    result = store.save('friendly', 'title', '', 'new text')
    assert result.content == 'new text' and result.path == path
    assert not (tmp_path / 'friendly.md').exists()
    store.reload()
    assert store.find('friendly').content == 'new text'


def test_failed_atomic_replace_preserves_file_and_runtime(tmp_path, monkeypatch):
    store = PersonaStore([tmp_path])
    original = store.save('friendly', '', '', 'original')
    raw = original.path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError('simulated full disk or unavailable destination')

    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError):
        store.save('friendly', '', '', 'new draft')
    assert original.path.read_bytes() == raw
    assert store.find('friendly').revision == original.revision
    assert not list(tmp_path.glob('.persona-*.tmp'))


@pytest.mark.parametrize('name', ['README', 'index', 'example'])
def test_reserved_directory_names_are_rejected_without_writing(tmp_path, name):
    store = PersonaStore([tmp_path])
    with pytest.raises(ValueError, match='保留'):
        store.save(name, '', '', 'text')
    assert not list(tmp_path.iterdir())
