import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.storage import Storage
from fllbot.storage.db import MIGRATIONS, SCHEMA_VERSION
from helpers import build_bot, make_bot

AUTH = {"Authorization": "Bearer admin-token"}


def test_stale_group_edits_are_rejected_without_changes_and_survive_restart(tmp_path: Path) -> None:
    bot = build_bot(tmp_path)
    with TestClient(create_app(bot)) as client:
        old = client.get('/api/console/groups/123', headers=AUTH).json()['revision']
        headers = {**AUTH, 'If-Match': old}
        result = client.patch('/api/console/groups/123', headers=headers, json={'enabled': False})
        assert result.status_code == 200
        latest = result.json()['revision']
        assert latest != old
        history = client.get('/api/console/groups/123/changes', headers=AUTH).json()
        assert client.patch('/api/console/groups/123', headers=headers, json={'enabled': True}).status_code == 409
        assert client.delete('/api/console/groups/123/rule', headers=headers).status_code == 409
        assert client.patch('/api/console/groups/123/access', headers=headers, json={'access': 'allow'}).status_code == 409
        assert client.get('/api/console/groups/123/changes', headers=AUTH).json() == history
        assert client.get('/api/console/groups/123', headers=AUTH).json()['enabled'] is False
        assert client.get('/api/console/groups/123/changes').status_code == 401
    with TestClient(create_app(build_bot(tmp_path))) as client:
        assert client.get('/api/console/groups/123', headers=AUTH).json()['revision'] == latest
        assert client.get('/api/console/groups/123/changes', headers=AUTH).json() == history


async def test_concurrent_console_writers_only_one_succeeds(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            revision = (await client.get('/api/console/groups/123', headers=AUTH)).json()['revision']
            responses = await asyncio.gather(*[
                client.patch('/api/console/groups/123', headers={**AUTH, 'If-Match': revision}, json={'enabled': enabled})
                for enabled in (True, False)
            ])
            assert sorted(response.status_code for response in responses) == [200, 409]
            changes = (await client.get('/api/console/groups/123/changes', headers=AUTH)).json()['items']
            assert len(changes) == 1
    finally:
        await bot.stop()


def test_batch_preflight_and_default_access_revision(tmp_path: Path) -> None:
    with TestClient(create_app(build_bot(tmp_path))) as client:
        snapshots = {gid: client.get(f'/api/console/groups/{gid}', headers=AUTH).json()['revision'] for gid in (123, 456)}
        access = client.get('/api/console/access', headers=AUTH).json()
        client.patch('/api/console/groups/123', headers=AUTH, json={'enabled': False})
        response = client.patch('/api/console/groups/batch-access', headers=AUTH, json={
            'group_ids': [123, 456], 'access': 'deny', 'revisions': snapshots,
        })
        assert response.status_code == 409
        assert client.get('/api/console/groups/456', headers=AUTH).json()['access'] == 'inherit'
        response = client.patch('/api/console/access', headers={**AUTH, 'If-Match': access['revision']}, json={'mode': 'listed'})
        assert response.status_code == 200
        assert client.patch('/api/console/access', headers={**AUTH, 'If-Match': access['revision']}, json={'mode': 'all'}).status_code == 409
        latest = client.get('/api/console/groups/456', headers=AUTH).json()['revision']
        assert latest != snapshots[456]


async def test_audit_is_atomic_redacted_and_paginated(tmp_path: Path) -> None:
    storage = Storage(tmp_path / 'audit.db')
    await storage.connect()
    try:
        await storage.upsert_group(123, enabled=True, config={'policy': {'system_extra': 'private-secret'}})
        await storage.upsert_group(123, enabled=False, config={'llm': {'enabled': False}})
        # Trigger records participate in the exact same SQL transaction as the write.
        await storage.db.execute('UPDATE groups SET enabled = 1 WHERE group_id = 123')
        await storage.db.rollback()
        result = await storage.group_changes(123, limit=1)
        assert len(result['items']) == 1
        assert set(result['items'][0]['fields']) == {'enabled', 'config.policy', 'config.llm'}
        assert 'private-secret' not in json.dumps(result)
        older = await storage.group_changes(123, before=result['next_cursor'], limit=1)
        assert older['items'][0]['action'] == 'created'
        assert older['next_cursor'] is None
        assert (await storage.list_group_states())[123]['enabled'] is False
        assert (await storage.group_changes(456))['items'] == []
    finally:
        await storage.close()


async def test_upgrade_from_schema_six_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as connection:
        for version in sorted(MIGRATIONS):
            if version <= 6:
                for statement in MIGRATIONS[version]:
                    connection.execute(statement)
        connection.execute('PRAGMA user_version = 6')
        connection.execute("INSERT INTO groups VALUES(123, 0, '{}', 0, 0)")
    storage = Storage(path)
    await storage.connect()
    try:
        assert await storage.schema_version() == SCHEMA_VERSION
        assert (await storage.list_group_states())[123]['enabled'] is False
        assert (await storage.group_changes(123))['items'] == []
        await storage.upsert_group(123, enabled=True)
        assert len((await storage.group_changes(123))['items']) == 1
    finally:
        await storage.close()
