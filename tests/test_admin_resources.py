from pathlib import Path

import httpx

from fllbot.app import create_app
from helpers import FakeConnection, make_bot, seed_personas

AUTH = {"Authorization": "Bearer admin-token"}
LLM = {
    "enabled": True, "api_key": "fake-secret", "default": "normal", "persona": "friendly",
    "providers": {"normal": {"model": "normal"}, "other": {"model": "other"}},
    "routes": {"group": "normal"}, "fallback_providers": ["normal"],
}


async def test_resource_references_and_persona_deletion_guard(tmp_path: Path) -> None:
    seed_personas(tmp_path, "friendly", "other")
    bot = await make_bot(tmp_path, llm=LLM)
    await bot.bots.update(10001, nickname="测试账号", defaults={"persona": "friendly", "provider": "normal"})
    await bot.permissions.save_group(123, enabled=True, config={"llm": {"persona": "friendly"}, "policy": {"provider": "normal"}})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url="http://test", headers=AUTH) as client:
            response = await client.get('/api/console/resources/persona/friendly/references')
            assert response.status_code == 200
            assert [item['scope'] for item in response.json()['items']] == ['system', 'bot', 'group']
            assert 'fake-secret' not in response.text
            models = (await client.get('/api/console/resources/provider/normal/references')).json()['items']
            assert {item['field'] for item in models} == {'llm.default', 'llm.routes.group', 'llm.fallback_providers', 'provider', 'policy.provider'}
            assert (await client.delete('/api/personas/friendly')).status_code == 409
            assert bot.personas.find('friendly') is not None
            assert bot.config.llm.persona == 'friendly'
            await bot.bots.update(10001, defaults={})
            await bot.permissions.save_group(123, enabled=True, config={})
            # Default-only deletion retains the existing explicit fallback behavior.
            deleted = await client.delete('/api/personas/friendly')
            assert deleted.status_code == 200
            assert deleted.json()['default'] == ''
            assert bot.personas.find('friendly') is None
            assert (await client.get('/api/console/resources/persona/friendly/references')).status_code == 404
            assert (await client.get('/api/console/resources/provider/missing/references')).status_code == 404
            assert (await client.get('/api/console/resources/unknown/missing/references')).status_code == 422
    finally:
        await bot.stop()


async def test_group_attention_distinguishes_intentional_off_and_bot_context(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, llm=LLM)
    online = FakeConnection(10001)
    bot.manager.add(online)
    await bot.bots.update(10001, defaults={"reply_mode": "at"})
    await bot.bots.update(10002, defaults={"reply_mode": "at", "provider": "other"})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url="http://test", headers=AUTH) as client:
            online_group = (await client.get('/api/console/groups?group_ids=123&self_id=10001')).json()['items'][0]
            assert online_group['diagnostic_status'] == 'ready'
            assert online_group['has_override'] is False
            offline_group = (await client.get('/api/console/groups?group_ids=123&self_id=10002')).json()['items'][0]
            assert offline_group['diagnostic_status'] == 'attention'
            assert offline_group['needs_attention'] is True
            assert offline_group['effective_policy']['provider'] == 'other'
            edited = await client.patch('/api/console/groups/123?self_id=10002', json={"enabled": True, "policy": {"auto_group": False}})
            assert edited.json()['self_id'] == 10002
            assert edited.json()['needs_attention'] is False
            assert edited.json()['diagnostic_status'] == 'disabled'
            await bot.permissions.save_group(123, enabled=True, config={'policy': {'auto_group': True, 'allowed_tools': ['gone__tool']}})
            assert (await client.get('/api/console/groups/123?self_id=10001')).json()['needs_attention'] is True
            await bot.update_config({'permission': {'deny_groups': [123]}})
            assert (await client.get('/api/console/groups/123?self_id=10001')).json()['needs_attention'] is False
            assert (await client.get('/api/console/groups?group_ids=123&self_id=0')).status_code == 422
    finally:
        await bot.stop()


async def test_resource_reference_api_requires_auth_and_offline_context_is_retained(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, llm=LLM)
    try:
        await bot.bots.update(10002, defaults={"reply_mode": "at"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url="http://test") as client:
            assert (await client.get('/api/console/resources/provider/normal/references')).status_code == 401
            data = (await client.get('/api/console/groups/123', headers=AUTH)).json()
            assert data['self_id'] == 10002
            assert data['needs_attention'] is True
    finally:
        await bot.stop()
