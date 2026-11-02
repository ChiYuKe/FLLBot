"""Account defaults stay isolated and durable across conflicts and failures."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from fllbot.app import create_app
from helpers import FakeConnection, make_bot, seed_personas

AUTH = {'Authorization': 'Bearer admin-token'}
URL = '/api/console/bots/10001/defaults'


async def test_account_defaults_version_choices_concurrency_and_inheritance(tmp_path):
    seed_personas(tmp_path, 'calm')
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake-provider-secret'})
    await bot.bots.update(10001, nickname='first', defaults={'reply_mode': 'at'})
    await bot.bots.update(10002, nickname='offline', defaults={'reply_mode': 'prefix', 'wake_word': 'other'})
    await bot.permissions.save_group(123, enabled=True, config={'policy': {'auto_group': True}, 'keep': 1})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            assert (await client.get(URL)).status_code == 401
            data = await client.get(URL, headers=AUTH)
            row = data.json()
            assert data.headers['etag'] == row['revision']
            assert not row['online'] and row['providers'] == ['default']
            assert row['personas'][0]['name'] == 'calm'
            assert 'fake-provider-secret' not in data.text
            replies = await asyncio.gather(*[
                client.put(URL, headers={**AUTH, 'If-Match': row['revision']}, json={'reply_mode': 'prefix', 'wake_word': wake, 'persona': 'calm'})
                for wake in ('first draft', 'second draft')
            ])
            assert sorted(reply.status_code for reply in replies) == [200, 409]
            current = (await client.get(URL, headers=AUTH)).json()
            assert current['revision'] != row['revision']
            assert bot.bots.defaults(10002).wake_word == 'other'
            assert bot.permissions.group_config(123) == {'policy': {'auto_group': True}, 'keep': 1}
            reset = await client.put(URL, headers={**AUTH, 'If-Match': current['revision']}, json={})
            assert reset.status_code == 200 and reset.json()['defaults']['reply_mode'] == 'inherit'
            assert bot.bots.get(10001)['nickname'] == 'first'
            assert bot.bots.defaults(10001).wake_word == ''
            assert (await client.get('/api/console/bots/99999/defaults', headers=AUTH)).status_code == 404
            assert (await client.put(URL, headers=AUTH, json={'persona': 'gone'})).status_code == 400
            audit = await (await bot.storage.db.execute("SELECT fields FROM management_changes WHERE action='bot_profile_updated'")).fetchall()
            assert audit and 'first draft' not in str([item[0] for item in audit])
            assert 'defaults.wake_word' in str([item[0] for item in audit])
    finally:
        await bot.stop()


async def test_nickname_sync_and_snapshot_reads_do_not_override_defaults(tmp_path):
    bot = await make_bot(tmp_path)
    conn = FakeConnection(10001)
    conn.get_login_info = AsyncMock(return_value={'user_id': 10001, 'nickname': 'new nickname'})
    bot.manager.add(conn)
    try:
        await bot.bots.update(10001, defaults={'reply_mode': 'prefix', 'wake_word': 'explicit'})
        revision = bot.bots.revision(10001)
        copied = bot.bots.get(10001)
        copied['defaults']['wake_word'] = 'must not publish'
        assert bot.bots.defaults(10001).wake_word == 'explicit'
        await bot.bots.sync(conn)
        assert bot.bots.revision(10001) == revision
        assert bot.bots.get(10001)['nickname'] == 'new nickname'
        assert bot.bots.defaults(10001).wake_word == 'explicit'
    finally:
        await bot.stop()


async def test_profile_and_audit_failure_roll_back_without_publication(tmp_path):
    bot = await make_bot(tmp_path)
    await bot.bots.update(10001, nickname='keep', defaults={'reply_mode': 'at'})
    try:
        before = bot.bots.get(10001)
        revision = bot.bots.revision(10001)
        await bot.storage.db.execute("CREATE TRIGGER fail_profile_audit BEFORE INSERT ON management_changes WHEN NEW.action='bot_profile_updated' BEGIN SELECT RAISE(ABORT,'profile rollback'); END")
        await bot.storage.db.commit()
        with pytest.raises(Exception, match='profile rollback'):
            await bot.bots.update(10001, defaults={'reply_mode': 'off'})
        assert bot.bots.get(10001) == before and bot.bots.revision(10001) == revision
        assert await bot.storage.kv_get('bot-profiles', '10001') == before
    finally:
        await bot.stop()


async def test_cancelled_save_publishes_committed_defaults_and_restart_retains_them(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path)
    await bot.bots.update(10001, nickname='keep')
    original = bot.storage.save_bot_profile
    committed, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        await original(*args, **kwargs)
        committed.set()
        await release.wait()

    monkeypatch.setattr(bot.storage, 'save_bot_profile', delayed)
    save = asyncio.create_task(bot.bots.update(10001, defaults={'reply_mode': 'prefix', 'wake_word': 'durable'}))
    try:
        await asyncio.wait_for(committed.wait(), 2)
        save.cancel()
        await asyncio.sleep(0)
        assert not save.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await save
    assert bot.bots.defaults(10001).wake_word == 'durable'
    await bot.stop()
    restarted = await make_bot(tmp_path)
    try:
        assert restarted.bots.defaults(10001).wake_word == 'durable'
        assert restarted.bots.get(10001)['nickname'] == 'keep'
    finally:
        await restarted.stop()
