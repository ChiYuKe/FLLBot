"""Atomic replacement uses isolated configuration and never calls a model or QQ."""

import asyncio
from pathlib import Path

import httpx
import pytest

from fllbot.app import create_app
from fllbot.storage import resources as resource_store
from helpers import make_bot, seed_personas

AUTH = {'Authorization': 'Bearer admin-token'}
LLM = {'enabled': True, 'api_key': 'fake-secret-not-for-output', 'default': 'old', 'persona': 'old',
       'providers': {'old': {'model': 'old'}, 'new': {'model': 'new'}},
       'routes': {'group': 'old', 'summary': 'new'}, 'fallback_providers': ['old', 'new']}


async def setup(bot):
    await bot.bots.update(10001, nickname='account', defaults={'provider': 'old', 'persona': 'old', 'wake_word': 'keep'})
    await bot.permissions.save_group(123, enabled=False, config={'policy': {'provider': 'old', 'auto_group': False},
                                                               'llm': {'persona': 'old'}, 'other': {'keep': 7}})


@pytest.mark.parametrize('kind', ['provider', 'persona'])
async def test_replacement_is_atomic_preserves_other_fields_and_survives_restart(tmp_path: Path, kind):
    seed_personas(tmp_path, 'old', 'new')
    bot = await make_bot(tmp_path, llm=LLM)
    await setup(bot)
    raw = (tmp_path/'personas'/'old.md').read_bytes()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            url = f'/api/console/resources/{kind}/old/replacement-preview'
            assert (await client.post(url, json={'target': 'new'})).status_code == 401
            preview = await client.post(url, headers=AUTH, json={'target': 'new'})
            assert preview.status_code == 200
            data = preview.json()
            assert data['can_execute'] and data['count'] == (5 if kind == 'provider' else 3)
            assert bot.bots.defaults(10001).model_dump()[kind] == 'old'
            assert 'fake-secret-not-for-output' not in preview.text
            payload = {'preview_id': data['id'], 'confirm': True}
            assert (await client.post('/api/console/resources/replacements', json=payload)).status_code == 401
            assert (await client.post('/api/console/resources/replacements', headers=AUTH, json={**payload, 'confirm': False})).status_code == 422
            response = await client.post('/api/console/resources/replacements', headers=AUTH, json=payload)
            assert response.status_code == 200 and response.json()['count'] == data['count']
            assert 'fake-secret-not-for-output' not in response.text
            assert (await client.post('/api/console/resources/replacements', headers=AUTH, json=payload)).status_code == 409
            assert bot.bots.defaults(10001).model_dump()[kind] == 'new'
            assert bot.bots.defaults(10001).wake_word == 'keep'
            group = bot.permissions.group_config(123)
            assert group['llm' if kind == 'persona' else 'policy'][kind] == 'new'
            assert not bot.permissions.group_enabled(123) and group['other'] == {'keep': 7}
            assert group['policy']['auto_group'] is False
            if kind == 'provider':
                assert bot.config.llm.default == 'new'
                assert bot.config.llm.routes == {'group': 'new', 'summary': 'new'}
                assert bot.config.llm.fallback_providers == ['new']
                assert set(bot.config.llm.providers) == {'old', 'new'}
            else:
                assert bot.config.llm.persona == bot.personas.default_name == 'new'
            refs = (await client.get(f'/api/console/resources/{kind}/old/references', headers=AUTH)).json()
            assert not refs['items']
            assert (tmp_path/'personas'/'old.md').read_bytes() == raw
            audit = await bot.storage.db.execute("SELECT fields FROM management_changes WHERE action='resource_replaced'")
            assert f'{kind}:old->new' in (await audit.fetchone())[0]
    finally:
        await bot.stop()
    restarted = await make_bot(tmp_path, llm=LLM)
    try:
        assert restarted.bots.defaults(10001).model_dump()[kind] == 'new'
        assert restarted.permissions.group_config(123)['llm' if kind == 'persona' else 'policy'][kind] == 'new'
        assert restarted.config.llm.default == ('new' if kind == 'provider' else 'old')
        assert restarted.config.llm.persona == ('new' if kind == 'persona' else 'old')
    finally:
        await restarted.stop()


async def test_changes_expiry_missing_target_and_new_references_reject_preview(tmp_path):
    seed_personas(tmp_path, 'old', 'new')
    bot = await make_bot(tmp_path, llm=LLM)
    await setup(bot)
    try:
        preview = await bot.replacements.preview('persona', 'old', 'new')
        await bot.permissions.save_group(456, enabled=True, config={'llm': {'persona': 'old'}})
        with pytest.raises(ValueError, match='变化'):
            await bot.replacements.apply(preview['id'])
        assert bot.config.llm.persona == 'old'
        preview = await bot.replacements.preview('persona', 'old', 'new')
        (tmp_path/'personas'/'new.md').write_text('changed target', encoding='utf-8')
        with pytest.raises(ValueError, match='变化'):
            await bot.replacements.apply(preview['id'])
        preview = await bot.replacements.preview('persona', 'old', 'new')
        bot.replacements.previews[preview['id']]['result']['expires_at'] = 0
        with pytest.raises(ValueError, match='过期'):
            await bot.replacements.apply(preview['id'])
        with pytest.raises(ValueError):
            await bot.replacements.preview('persona', 'old', 'old')
        with pytest.raises(ValueError):
            await bot.replacements.preview('provider', 'old', 'missing')
        assert bot.bots.defaults(10001).persona == 'old'
    finally:
        await bot.stop()


async def test_database_failure_rolls_back_all_bindings_audit_and_runtime(tmp_path):
    bot = await make_bot(tmp_path, llm=LLM)
    await setup(bot)
    try:
        preview = await bot.replacements.preview('provider', 'old', 'new')
        before = bot.replacements.state()
        await bot.storage.db.execute("CREATE TRIGGER fail_replacement BEFORE INSERT ON management_changes WHEN NEW.action='resource_replaced' BEGIN SELECT RAISE(ABORT,'synthetic rollback'); END")
        await bot.storage.db.commit()
        with pytest.raises(Exception, match='synthetic rollback'):
            await bot.replacements.apply(preview['id'])
        assert bot.replacements.state() == before
        assert bot.config.llm.default == 'old'
        assert (await bot.storage.list_group_states())[123]['config']['policy']['provider'] == 'old'
        assert (await bot.storage.kv_get('bot-profiles', '10001'))['defaults']['provider'] == 'old'
        assert await bot.storage.meta_get('config_overrides', {}) == {}
        rows = await (await bot.storage.db.execute("SELECT count(*) FROM management_changes WHERE action='resource_replaced'")).fetchone()
        assert rows[0] == 0
    finally:
        await bot.stop()


async def test_only_local_bindings_do_not_persist_or_lose_runtime_config(tmp_path):
    bot = await make_bot(tmp_path, llm={**LLM, 'default': 'new', 'routes': {}, 'fallback_providers': []})
    await bot.bots.update(10001, defaults={'provider': 'old'})
    try:
        revision = bot.config_revision()
        runtime_overrides = bot.config_store.overrides
        preview = await bot.replacements.preview('provider', 'old', 'new')
        assert preview['count'] == 1
        await bot.replacements.apply(preview['id'])
        assert bot.config_revision() == revision
        assert bot.config_store.overrides == runtime_overrides
        assert await bot.storage.meta_get('config_overrides', {}) == {}
        preview = await bot.replacements.preview('provider', 'old', 'new')
        assert not preview['can_execute']
    finally:
        await bot.stop()


async def test_raw_database_change_and_reference_limit_reject_stale_preview(tmp_path):
    bot = await make_bot(tmp_path, llm=LLM)
    await setup(bot)
    try:
        preview = await bot.replacements.preview('provider', 'old', 'new')
        # Bypass the service cache to ensure the transaction also checks the database.
        await bot.storage.db.execute("UPDATE groups SET config='{}' WHERE group_id=123")
        await bot.storage.db.commit()
        with pytest.raises(ValueError, match='数据库配置已变化'):
            await bot.replacements.apply(preview['id'])
        assert bot.config.llm.default == 'old'
        await bot.permissions.load()
        bot.replacements.MAX_REFERENCES = 2
        preview = await bot.replacements.preview('provider', 'old', 'new')
        assert preview['truncated'] and not preview['can_execute']
        assert len(preview['items']) == 2
        with pytest.raises(ValueError, match='超过'):
            await bot.replacements.apply(preview['id'])
    finally:
        await bot.stop()


async def test_request_cancellation_after_commit_still_publishes_all_caches(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM)
    await setup(bot)
    committed, release = asyncio.Event(), asyncio.Event()
    original = resource_store.replace

    async def delayed(*args, **kwargs):
        result = await original(*args, **kwargs)
        committed.set()
        await release.wait()
        return result

    monkeypatch.setattr(resource_store, 'replace', delayed)
    try:
        preview = await bot.replacements.preview('provider', 'old', 'new')
        request = asyncio.create_task(bot.replacements.apply(preview['id']))
        await asyncio.wait_for(committed.wait(), 2)
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert bot.config.llm.default == 'new'
        assert bot.bots.defaults(10001).provider == 'new'
        assert bot.permissions.group_config(123)['policy']['provider'] == 'new'
        assert (await bot.storage.list_group_states())[123]['config']['policy']['provider'] == 'new'
    finally:
        release.set()
        await bot.stop()
