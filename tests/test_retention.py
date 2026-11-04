"""Retention never touches a real instance or external services."""

import time

import httpx
import pytest
from pydantic import ValidationError

from fllbot.app import create_app
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from fllbot.retention import CleanupRequest
from fllbot.storage import cleanup
from helpers import FakeConnection, group_message, make_bot

AUTH = {'Authorization': 'Bearer admin-token'}


async def seed_message(bot, *, self_id=1, scene='group', scene_id=10, age=40):
    await bot.storage.db.execute(
        'INSERT INTO message_log(self_id,scene,scene_id,user_id,content,created_at) VALUES(?,?,?,?,?,?)',
        (self_id, scene, scene_id, 7, 'private synthetic text', time.time()-age*86400),
    )
    await bot.storage.db.commit()


async def count(bot, table):
    return (await (await bot.storage.db.execute(f'SELECT count(*) FROM {table}')).fetchone())[0]


async def test_preview_confirm_scope_and_persistent_metadata(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        await seed_message(bot)
        await seed_message(bot, self_id=2)
        await seed_message(bot, scene='private')
        await seed_message(bot, age=0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            body = {'datasets': ['messages'], 'older_than_days': 30, 'scene': 'group', 'scene_id': 10, 'self_id': 1}
            assert (await client.post('/api/console/data-cleanup/preview', json=body)).status_code == 401
            preview = (await client.post('/api/console/data-cleanup/preview', headers=AUTH, json=body)).json()
            assert preview['counts'] == {'message_log': 1}
            assert 'signature' not in preview and 'private synthetic text' not in str(preview)
            assert await count(bot, 'message_log') == 4
            assert (await client.post('/api/console/data-cleanup', headers=AUTH, json={'preview_id': preview['id'], 'confirm': False})).status_code == 422
            submitted = await client.post('/api/console/data-cleanup', headers=AUTH, json={'preview_id': preview['id'], 'confirm': True})
            assert submitted.status_code == 202
            await bot.cleanup.task
            job = (await client.get('/api/console/data-cleanup/jobs/'+submitted.json()['id'], headers=AUTH)).json()
            assert job['status'] == 'completed' and job['result']['total'] == 1
            assert await count(bot, 'message_log') == 3
            assert (await client.post('/api/console/data-cleanup', headers=AUTH, json={'preview_id': preview['id'], 'confirm': True})).status_code == 409
            runs = (await client.get('/api/console/data-cleanup/runs', headers=AUTH)).json()['items']
            assert runs[0]['specification']['self_id'] == 1
            assert 'private synthetic text' not in str(runs)
    finally:
        await bot.stop()


async def test_changed_or_expired_preview_cannot_delete(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        await seed_message(bot)
        request = CleanupRequest(datasets=['messages'], older_than_days=30)
        preview = await bot.cleanup.preview(request)
        await seed_message(bot)
        job = bot.cleanup.start(preview['id'])
        await bot.cleanup.task
        assert bot.cleanup.jobs[job['id']]['status'] == 'conflict'
        assert await count(bot, 'message_log') == 2
        assert await count(bot, 'cleanup_runs') == 0
        preview = await bot.cleanup.preview(request)
        bot.cleanup.previews[preview['id']]['expires_at'] = 0
        with pytest.raises(ValueError, match='过期'):
            bot.cleanup.start(preview['id'])
        monkey_spec = request.specification()
        original_max = cleanup.MAX_ROWS
        try:
            cleanup.MAX_ROWS = 1
            assert not (await cleanup.preview(bot.storage, monkey_spec))['can_execute']
            with pytest.raises(cleanup.CleanupChangedError):
                await cleanup.execute(bot.storage, monkey_spec)
        finally:
            cleanup.MAX_ROWS = original_max
        assert await count(bot, 'message_log') == 2
    finally:
        await bot.stop()


async def test_private_cleanup_never_selects_unrelated_management_changes(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        old = time.time() - 40 * 86400
        await bot.storage.db.executemany(
            'INSERT INTO management_changes(group_id,action,fields,created_at) VALUES(?,?,?,?)',
            [(0, 'global_old', '[]', old), (123, 'group_old', '[]', old)],
        )
        await bot.storage.db.commit()
        cutoff = time.time() - 30 * 86400

        private = await cleanup.preview(bot.storage, {
            'cutoffs': {'changes': cutoff}, 'scene': 'private', 'scene_id': 987,
        })
        assert private['counts'] == {'management_changes': 0}

        group_spec = {'cutoffs': {'changes': cutoff}, 'scene': 'group', 'scene_id': 123}
        group = await cleanup.preview(bot.storage, group_spec)
        assert group['counts'] == {'management_changes': 1}
        result = await cleanup.execute(bot.storage, group_spec, expected=group['signature'])
        assert result['total'] == 1
        cursor = await bot.storage.db.execute('SELECT group_id,action FROM management_changes')
        assert [tuple(row) for row in await cursor.fetchall()] == [(0, 'global_old')]
    finally:
        await bot.stop()


async def test_cleanup_invalidates_linked_summary_and_inflight_context(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        store = bot.storage.memory
        old = await store.append(10001, 20002, 'user', 'old', created_at=time.time()-40*86400)
        await store.append(10001, 20002, 'user', 'new', created_at=time.time())
        sources = await store.recent(10001, 20002, 100000)
        await store.save_summary(10001, 20002, sources, '1111', [{'topic': 'old and new'}])
        await store.append(10001, 99, 'user', 'keep other group', created_at=1)
        conn = FakeConnection()
        ctx = Context(bot, conn, parse_event(group_message('hello')))
        epoch = ctx.data_epoch
        spec = CleanupRequest(datasets=['conversations'], older_than_days=30, scene='group', scene_id=20002).specification()
        preview = await cleanup.preview(bot.storage, spec)
        assert preview['counts']['group_memory_summaries'] == 1
        assert preview['counts']['group_memory_messages'] == 1
        await cleanup.execute(bot.storage, spec, expected=preview['signature'])
        assert not ctx.data_current
        assert await ctx.send('stale reply') is None and not conn.sent
        await bot.storage.add_chat_message('group', 20002, 'assistant', 'stale', generation=epoch)
        assert await store.append(10001, 20002, 'assistant', 'stale', generation=epoch, created_at=time.time()) == 0
        # The last source still exists; checking only the last row would resurrect this summary.
        await store.save_summary(10001, 20002, sources, '1111', [{'topic': 'stale summary'}])
        assert await count(bot, 'group_memory_summaries') == 0
        assert await count(bot, 'chat_messages') == 0
        assert [row['id'] for row in await store.recent(10001, 20002, 100000)] == [old+1]
        assert len(await store.recent(10001, 99, 100000)) == 1
    finally:
        await bot.stop()


async def test_policy_revision_default_disabled_and_automatic_batch(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        await seed_message(bot)
        with pytest.raises(ValueError):
            bot.cleanup.run_automatic()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test') as client:
            current = (await client.get('/api/console/data-retention', headers=AUTH)).json()
            assert not current['policy']['enabled'] and current['policy']['messages_days'] == 0
            updated = await client.put('/api/console/data-retention', headers={**AUTH, 'If-Match': current['revision']}, json={**current['policy'], 'enabled': True, 'messages_days': 30})
            assert updated.status_code == 200
            assert (await client.put('/api/console/data-retention', headers={**AUTH, 'If-Match': current['revision']}, json=current['policy'])).status_code == 409
            assert await count(bot, 'message_log') == 1  # Saving never deletes immediately.
        job = bot.cleanup.run_automatic()
        await bot.cleanup.task
        assert bot.cleanup.jobs[job['id']]['result']['total'] == 1
        assert (await bot.cleanup.runs())['items'][0]['kind'] == 'automatic'
        assert await count(bot, 'meta') > 0
    finally:
        await bot.stop()


async def test_automatic_batch_and_failure_roll_back(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        await bot.storage.db.executemany(
            "INSERT INTO message_log(self_id,scene,scene_id,user_id,content,created_at) VALUES(1,'group',10,7,'test',1)",
            [()] * 5001,
        )
        await bot.storage.db.commit()
        spec = CleanupRequest(datasets=['messages'], older_than_days=30).specification()
        result = await cleanup.execute(bot.storage, spec, kind='automatic', batch=5000)
        assert result['total'] == 5000 and await count(bot, 'message_log') == 1
        await bot.storage.db.execute("CREATE TRIGGER cleanup_fail BEFORE INSERT ON cleanup_runs BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        await bot.storage.db.commit()
        preview = await bot.cleanup.preview(CleanupRequest(datasets=['messages'], older_than_days=30))
        job = bot.cleanup.start(preview['id'])
        await bot.cleanup.task
        assert bot.cleanup.jobs[job['id']]['status'] == 'failed'
        assert await count(bot, 'message_log') == 1
        assert await count(bot, 'cleanup_runs') == 1
    finally:
        await bot.stop()


@pytest.mark.parametrize('patch', [{'scene_id': 10}, {'datasets': ['usage'], 'self_id': 1}, {'older_than_days': 0}, {'datasets': ['unknown']}])
def test_invalid_scope(patch):
    with pytest.raises(ValidationError):
        CleanupRequest.model_validate({'datasets': ['messages'], 'older_than_days': 30, **patch})


async def test_candidate_cleanup_is_scoped_and_invalidates_inflight(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        epoch = bot.storage.conversation_generation
        for account in (11, 22):
            await bot.storage.prepare_delivery(str(account), account, 'group', 123, 'private candidate', ['part'], epoch)
        spec = {'scene': 'group', 'scene_id': 123, 'self_id': 11,
                'cutoffs': {'conversations': time.time()+1}}
        preview = await cleanup.preview(bot.storage, spec)
        assert preview['counts']['reply_deliveries'] == 1
        assert preview['counts']['delivery_parts'] == 1
        await cleanup.execute(bot.storage, spec, expected=preview['signature'])
        assert await count(bot, 'reply_deliveries') == 2
        assert await count(bot, 'delivery_parts') == 2
        cleared = await (await bot.storage.db.execute("SELECT candidate,metadata,bodies_cleared FROM reply_deliveries WHERE self_id=11")).fetchone()
        assert tuple(cleared) == ('','{}',1)
        assert (await cleanup.preview(bot.storage,spec))['counts']['reply_deliveries'] == 0
        assert bot.storage.conversation_generation == epoch
        assert bot.storage.scope_generation(11,'group',123) == 1
        assert bot.storage.scope_generation(22,'group',123) == 0
        assert not await bot.storage.prepare_delivery('old',11,'group',123,'stale',['stale'],epoch,metadata={'scope_epoch':0})
    finally:
        await bot.stop()
