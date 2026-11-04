from unittest.mock import AsyncMock

import httpx

from fllbot.app import create_app
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


async def test_decision_is_structured_versioned_and_contains_no_stored_body(tmp_path):
    bot = await make_bot(tmp_path,llm={'enabled':True,'api_key':'fake'})
    try:
        await bot.permissions.save_group(20002,enabled=True,config={'policy':{'auto_group':True,'trigger_modes':['at'],'cooldown_seconds':0}})
        ctx = Context(bot,FakeConnection(),parse_event(group_message('private synthetic words')))
        first = bot.trigger_policy.decide(ctx)
        assert (first.outcome,first.reason) == ('ignore','trigger_not_matched')
        await bot.storage.record_trigger(ctx,first)
        await bot.update_config({'chat':{'wake_word':'changed'}})
        assert bot.trigger_policy.decide(ctx).effective_policy_version != first.effective_policy_version
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)),base_url='http://test') as client:
            assert (await client.get('/api/console/trigger-decisions')).status_code == 401
            data = (await client.get('/api/console/trigger-decisions?self_id=10001',headers={'Authorization':'Bearer admin-token'})).json()
            assert data['items'][0]['reason'] == 'trigger_not_matched'
            assert 'private synthetic words' not in str(data)
    finally:
        await bot.stop()


async def test_scene_denied_is_persisted_before_memory_or_model(tmp_path):
    bot = await make_bot(tmp_path,llm={'enabled':True,'api_key':'fake'})
    try:
        await bot.permissions.set_group_enabled(20002,False)
        bot.chat.complete = AsyncMock()
        await bot.dispatcher.dispatch(parse_event(group_message('denied source')),FakeConnection())
        bot.chat.complete.assert_not_awaited()
        assert not await bot.chat.memory.store.recent(10001,20002,9999)
        decisions = await bot.storage.list_trigger_decisions(self_id=10001)
        assert decisions['items'][0]['outcome'] == 'reject'
        assert decisions['items'][0]['reason'] == 'scene_denied'
    finally:
        await bot.stop()
