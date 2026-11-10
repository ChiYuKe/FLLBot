from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from fllbot.app import create_app
from fllbot.llm.chat import ChatReply
from fllbot.llm.context_builder import ContextBuilder
from helpers import make_bot


async def test_quote_provenance_stays_with_current_message_when_plugin_policies_are_present():
    assemble = AsyncMock(return_value=[{'role': 'system', 'content': 'persona'},
                                      {'role': 'user', 'content': '把这个发成文件'}])
    config = SimpleNamespace(model_dump_json=lambda: '{}')
    context = SimpleNamespace(self_id=11, quoted_message=SimpleNamespace(
        message_id=42, user_id=99, error=''), forward_metadata={})
    builder = ContextBuilder(assemble, lambda: config, lambda *args: ('default', 'model'))
    snapshot = await builder.build('group', 123, '把这个发成文件', ctx=context,
        agent_instructions=[{'source': 'text__file_delivery', 'text': 'file rules'}])
    sources = snapshot.sources()
    current = next(source for source in sources if source['kind'] == 'current')
    assert current['index'] == 1 and current['quote']['message_id'] == 42
    assert all('quote' not in source for source in sources if source['kind'] == 'plugin_instruction')


async def test_snapshot_is_independent_and_has_fixed_cutoff(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        captured = []
        async def assemble(scene, scene_id, text, **options):
            captured.append(options['history_before'])
            return [{'role':'system','content':'persona'}, {'role':'user','content':text}]
        builder = ContextBuilder(assemble, lambda:bot.config, lambda *args:('default','model'))
        snapshot = await builder.build('group', 123, 'hello')
        messages = snapshot.messages()
        messages[0]['content'] = 'changed'
        assert snapshot.messages()[0]['content'] == 'persona'
        assert captured == [snapshot.history_cutoff]
        assert snapshot.config_version and snapshot.persona_version
    finally:
        await bot.stop()


async def test_admin_preview_defaults_to_no_writes_and_explicit_save_is_legacy(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, builtin=False, llm={'enabled':True,'api_key':'fake','context_mode':'custom'})
    try:
        complete = AsyncMock(return_value=ChatReply('answer','default','model'))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            headers = {'Authorization':'Bearer admin-token'}
            body = {'scene':'group','scene_id':123,'message':'hello'}
            response = await client.post('/api/llm/chat', headers=headers, json=body)
            assert response.status_code == 200
            assert response.json()['text'] == 'answer'
            assert not await bot.storage.recent_chat('group',123)
            response = await client.post('/api/llm/chat', headers=headers, json={**body,'save':True})
            assert response.status_code == 200
        assert len(await bot.storage.recent_chat('group',123)) == 2
        assert not await bot.storage.recent_chat('group',123,self_id=11)
        row = await (await bot.storage.db.execute('SELECT count(*) FROM reply_deliveries')).fetchone()
        assert row[0] == 0
    finally:
        await bot.stop()
