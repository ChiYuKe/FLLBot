import httpx

from fllbot.app import create_app
from fllbot.retention import CleanupRequest
from helpers import make_bot

AUTH = {'Authorization':'Bearer admin-token'}


async def test_history_directory_and_clear_are_account_scoped(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    try:
        for account,text in [(0,'legacy'),(11,'one'),(22,'two')]:
            await bot.storage.add_chat_message('group',123,'user',text,self_id=account)
        for account,text in [(11,'one background'),(22,'two background')]:
            await bot.storage.memory.append(account,123,'user',text,created_at=100)
            await bot.storage.prepare_delivery(str(account),account,'group',123,text,[text],bot.storage.conversation_generation)
            await bot.storage.claim_tool('tool'+str(account),account,'group',123,1,'example','read_only',
                bot.storage.conversation_generation,'turn'+str(account),'call','hash')
            await bot.storage.finish_tool('tool'+str(account),'succeeded',output=text,generation=bot.storage.conversation_generation)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)),base_url='http://test') as client:
            rooms = (await client.get('/api/console/sessions',headers=AUTH)).json()['items']
            assert {room['self_id'] for room in rooms} == {0,11,22}
            legacy = (await client.get('/api/chat/group/123',headers=AUTH)).json()
            assert [row['content'] for row in legacy['messages']] == ['legacy']
            first = (await client.get('/api/chat/group/123?self_id=11',headers=AUTH)).json()
            assert first['source'] == 'memory'
            assert [row['content'] for row in first['messages']] == ['one background']
            result = await client.delete('/api/chat/group/123?self_id=11',headers=AUTH)
            assert result.status_code == 200
            assert [row['content'] for row in (await client.get('/api/chat/group/123?self_id=22',headers=AUTH)).json()['messages']] == ['two background']
        assert not await bot.storage.recent_chat('group',123,self_id=11)
        assert [row.content for row in await bot.storage.recent_chat('group',123,self_id=22)] == ['two']
        assert [row.content for row in await bot.storage.recent_chat('group',123)] == ['legacy']
        plans = await (await bot.storage.db.execute('SELECT self_id FROM reply_deliveries WHERE bodies_cleared=0')).fetchall()
        assert [row['self_id'] for row in plans] == [22]
        results = await (await bot.storage.db.execute('SELECT execution_id FROM tool_results')).fetchall()
        assert [row['execution_id'] for row in results] == ['tool22']
    finally:
        await bot.stop()


async def test_scoped_conversation_cleanup_request_is_valid(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    try:
        for account in (11,22):
            await bot.storage.memory.append(account,123,'user','old source',created_at=1)
        request = CleanupRequest(datasets=['conversations'],older_than_days=1,self_id=11,scene='group',scene_id=123)
        preview = await bot.cleanup.preview(request)
        assert preview['counts']['group_memory_messages'] == 1
        assert preview['specification']['self_id'] == 11
    finally:
        await bot.stop()
