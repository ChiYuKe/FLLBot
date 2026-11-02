from pathlib import Path

import httpx

from fllbot.app import create_app
from helpers import FakeConnection, make_bot

AUTH = {"Authorization": "Bearer admin-token"}


async def test_setup_guide_requires_auth_and_never_claims_real_verification(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "not-a-real-secret", "model": "demo"})
    conn = FakeConnection(10001)
    bot.manager.add(conn)
    await bot.bots.update(10001, nickname="测试账号", defaults={"reply_mode": "at"})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            assert (await client.get('/api/console/setup')).status_code == 401
            assert (await client.get('/api/console/setup?group_id=0', headers=AUTH)).status_code == 422
            initial = (await client.get('/api/console/setup', headers=AUTH)).json()
            assert initial['configuration_ready'] is False
            assert initial['group_id'] is None
            assert initial['self_id'] == 10001
            snapshot = bot.config.model_dump()
            groups = bot.permissions.list_groups()
            response = await client.get('/api/console/setup?self_id=10001&group_id=123', headers=AUTH)
            data = response.json()
            assert data['configuration_ready'] is True
            assert data['completed_checks'] == data['total_checks'] == 4
            assert data['verification']['status'] == 'manual'
            assert 'not-a-real-secret' not in response.text
            assert next(step for step in data['steps'] if step['id'] == 'persona')['optional'] is True
            assert next(step for step in data['steps'] if step['id'] == 'persona')['done'] is False
            assert bot.config.model_dump() == snapshot
            assert bot.permissions.list_groups() == groups
            assert conn.calls == []
            assert conn.sent == []
    finally:
        await bot.stop()


async def test_setup_guide_uses_selected_bot_and_group_hard_limits(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake-key", "model": "demo"})
    try:
        bot.manager.add(FakeConnection(10001))
        await bot.bots.update(10001, defaults={"reply_mode": "at"})
        await bot.bots.update(10002, defaults={"reply_mode": "off"})
        await bot.permissions.save_group(123, enabled=True, config={'llm': {'enabled': False}})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test', headers=AUTH) as client:
            data = (await client.get('/api/console/setup?self_id=10002&group_id=123')).json()
            steps = {step['id']: step for step in data['steps']}
            assert steps['connection']['done'] is False
            assert steps['access']['done'] is True
            assert steps['response']['done'] is False
            assert data['configuration_ready'] is False
            assert 'scene_ai_disabled' in steps['response']['blocked_codes']
            await bot.update_config({'permission': {'deny_groups': [123]}})
            data = (await client.get('/api/console/setup?self_id=10001&group_id=123')).json()
            assert next(step for step in data['steps'] if step['id'] == 'access')['done'] is False
    finally:
        await bot.stop()
