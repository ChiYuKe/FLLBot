"""Group-table persona updates reach the next natural-language model request without restart."""

import copy
from types import SimpleNamespace

import httpx

from fllbot.app import create_app
from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot


async def test_group_persona_switch_updates_next_request_and_survives_restart(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake', 'context_window': 16384})
    bot.personas.save('original', '原人格', '', 'ORIGINAL_PERSONA：你叫小白，语气正式。')
    bot.personas.save('playful', '新人格', '', 'PLAYFUL_PERSONA：你叫猫猫，说话活泼。')
    prompts = []

    async def complete(messages, **options):
        prompts.append(copy.deepcopy(messages))
        return SimpleNamespace(text='上一轮的正式回答' if len(prompts) == 1 else '新回答', tool_calls=[])

    monkeypatch.setattr(bot.chat, 'complete', complete)
    connection = FakeConnection()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            headers = {'Authorization': 'Bearer admin-token'}
            payload = {'enabled': True, 'llm_enabled': True, 'persona': 'original', 'policy': {
                'auto_group': True, 'trigger_modes': ['at'], 'cooldown_seconds': 0,
                'allowed_tools': ['basic__check_online']}}
            response = await client.patch('/api/console/groups/20002', headers=headers, json=payload)
            assert response.status_code == 200
            await bot.dispatcher.dispatch(parse_event(group_message('你好', at=10001)), connection)
            assert 'ORIGINAL_PERSONA' in prompts[0][0]['content']
            response = await client.patch('/api/console/groups/20002', headers=headers, json={
                'enabled': True, 'llm_enabled': True, 'persona': 'playful'})
            assert response.status_code == 200
            assert response.json()['persona'] == response.json()['effective_policy']['persona'] == 'playful'
            await bot.dispatcher.dispatch(parse_event(group_message('继续聊聊', message_id=2, at=10001)), connection)
            system = prompts[1][0]['content']
            assert 'PLAYFUL_PERSONA' in system and 'ORIGINAL_PERSONA' not in system
            assert '以本系统消息中的当前人格设定为准' in system
            assert '不延续与当前人格冲突的旧身份或语气' in system
            assert '上一轮的正式回答' in str(prompts[1][1:])
            assert bot.permissions.group_config(20002)['policy']['allowed_tools'] == ['basic__check_online']
            assert bot.chat.persona_name('group', 12345) != 'playful'
    finally:
        await bot.stop()
    restarted = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake', 'context_window': 16384})
    try:
        assert restarted.chat.persona_name('group', 20002) == 'playful'
        assert 'PLAYFUL_PERSONA' in restarted.chat.system_prompt('group', 20002)
    finally:
        await restarted.stop()
