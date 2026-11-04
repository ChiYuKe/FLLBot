"""Voice tasks use simulated synthesis and OneBot; no network or real messages."""
import asyncio
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.connection import ActionError
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


@pytest.fixture
async def voice_bot(tmp_path):
    directory = tmp_path / 'plugins'
    shutil.copytree(Path(__file__).resolve().parents[1] / 'plugins' / 'voice',directory / 'voice')
    bot = await make_bot(tmp_path,plugin_dirs=[directory],llm={'enabled':True,'enable_group':True,'api_key':'fake-test-key'})
    owner = bot.plugins.get('voice')
    spec = owner.ai_tools[0]
    module = sys.modules[spec.callback.__module__]
    try:
        yield bot,owner,spec,module
    finally:
        await bot.stop()


def context(bot, owner, connection=None, message_id=1):
    return Context(bot,connection or FakeConnection(),parse_event(group_message('念出来',message_id=message_id)),plugin=owner)


async def test_tts_management_requires_configured_admin_qq(voice_bot,monkeypatch):
    bot,owner,_,module = voice_bot
    stop = AsyncMock()
    monkeypatch.setattr(module,'_stop_owned_qwen',stop)
    ordinary = context(bot,owner)
    ordinary.event.sender.role = 'owner'
    result = await module.set_service_enabled(ordinary,False)
    assert result['error'] == 'permission_denied'
    assert owner.settings.get('service_enabled') is True
    stop.assert_not_awaited()
    tool = next(spec for spec in owner.ai_tools if spec.name == 'set_service_enabled')
    assert tool.permission == 'superuser' and tool.effect == 'idempotent'


async def test_tts_admin_switch_is_global_and_blocks_queued_speech(voice_bot,monkeypatch):
    bot,owner,_,module = voice_bot
    stop = AsyncMock()
    monkeypatch.setattr(module,'_stop_owned_qwen',stop)
    admin = Context(bot,FakeConnection(),parse_event(group_message('关闭TTS',user_id=99999)),plugin=owner)
    assert (await module.set_service_enabled(admin,False))['enabled'] is False
    stop.assert_awaited_once()
    await owner.settings.set('enabled',True,group_id=20002)
    assert module._snapshot(context(bot,owner))['enabled'] is False
    with pytest.raises(RuntimeError,match='voice_context_expired'):
        module._require_current(context(bot,owner))
    assert (await module.set_service_enabled(admin,True))['enabled'] is True
    assert module._snapshot(context(bot,owner))['enabled'] is True


async def execute(bot,owner,spec,ctx):
    ctx.turn_input = ctx.text
    async with bot.conversations.turn(ctx):
        return await bot.tool_executor.execute(ctx,spec.qualified_name,{'text':'测试语音'},
                                               {spec.qualified_name:(owner,spec)})


async def test_voice_tool_waits_for_durable_receipt(voice_bot,tmp_path,monkeypatch):
    bot,owner,spec,module = voice_bot
    entered,release = asyncio.Event(),asyncio.Event()
    audio = tmp_path / 'sample.wav'
    audio.write_bytes(b'fake-audio')
    async def synthesize(*args,**kwargs):
        entered.set()
        await release.wait()
        return audio
    monkeypatch.setattr(module,'_request_tts',synthesize)
    ctx = context(bot,owner)
    task = asyncio.create_task(execute(bot,owner,spec,ctx))
    try:
        await asyncio.wait_for(entered.wait(),1)
        assert not task.done() and not ctx.connection.sent
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert detail['tools'][0]['status'] == 'running'
        release.set()
        result,silent = await asyncio.wait_for(task,2)
        assert json.loads(result)['completed'] and silent
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert detail['tools'][0]['status'] == 'succeeded'
        assert detail['status'] == 'delivered'
        rows = await (await bot.storage.db.execute('SELECT status,message_id FROM delivery_parts')).fetchall()
        assert [(row['status'],row['message_id']) for row in rows] == [('sent','1001')]
        assert await bot.storage.recovery_plan(ctx.turn_id) is None
        history = await (await bot.storage.db.execute(
            'SELECT role,content FROM chat_messages WHERE self_id=? AND scene=? AND scene_id=? ORDER BY id',
            (ctx.self_id,'group',ctx.group_id))).fetchall()
        assert any(item['role']=='assistant' and item['content']=='测试语音' for item in history)
        assert [item['role'] for item in history] == ['user','assistant']
    finally:
        release.set()
        await asyncio.gather(task,return_exceptions=True)


async def test_agent_voice_projects_only_acknowledged_audio_once(voice_bot,tmp_path,monkeypatch):
    bot,owner,spec,module = voice_bot
    audio = tmp_path/'sample.wav'
    audio.write_bytes(b'fake-audio')
    async def synthesize(*args,**kwargs):
        return audio
    monkeypatch.setattr(module,'_request_tts',synthesize)
    complete = AsyncMock(return_value=SimpleNamespace(text='',tool_calls=[{
        'id':'voice-call','type':'function','function':{'name':spec.qualified_name,'arguments':'{"text":"测试语音"}'}}]))
    monkeypatch.setattr(bot.chat,'complete',complete)
    await bot.permissions.save_group(20002,enabled=True,config={'policy':{
        'auto_group':True,'trigger_modes':['all'],'cooldown_seconds':0,'allowed_tools':[spec.qualified_name]}})
    connection = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message('念出来')),connection)
    complete.assert_awaited_once()
    history = await bot.storage.recent_chat('group',20002,self_id=10001)
    assert [(item.role,item.content) for item in history] == [('user','念出来'),('assistant','测试语音')]
    assert len(connection.sent) == 1


@pytest.mark.parametrize('revoke',['plugin','group','voice_setting','clear','account'])
async def test_voice_rechecks_after_synthesis_before_cache_and_send(voice_bot,tmp_path,monkeypatch,revoke):
    bot,owner,spec,module = voice_bot
    entered,release = asyncio.Event(),asyncio.Event()
    audio = tmp_path / 'sample.wav'
    audio.write_bytes(b'fake-audio')
    async def synthesize(*args,**kwargs):
        entered.set()
        await release.wait()
        return audio
    monkeypatch.setattr(module,'_request_tts',synthesize)
    ctx = context(bot,owner)
    task = asyncio.create_task(execute(bot,owner,spec,ctx))
    try:
        await asyncio.wait_for(entered.wait(),1)
        if revoke == 'plugin':
            await bot.permissions.set_plugin_enabled('voice',False)
        elif revoke == 'group':
            await bot.permissions.set_group_enabled(ctx.group_id,False)
        elif revoke == 'voice_setting':
            await owner.settings.update({'enabled':False},group_id=ctx.group_id)
        elif revoke == 'clear':
            await bot.storage.clear_chat('group',ctx.group_id,self_id=ctx.self_id)
        else:
            await bot.bots.update(ctx.self_id,enabled=False)
        release.set()
        await asyncio.wait_for(asyncio.gather(task,return_exceptions=True),2)
        assert not ctx.connection.sent
        assert not (tmp_path/'data'/'tts').exists()
        count = await (await bot.storage.db.execute('SELECT count(*) FROM delivery_parts')).fetchone()
        assert count[0] == 0
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert detail['tools'][0]['status'] == 'uncertain'
    finally:
        release.set()
        await asyncio.gather(task,return_exceptions=True)


async def test_cancel_inflight_voice_preserves_uncertain_receipt(voice_bot,tmp_path,monkeypatch):
    bot,owner,spec,module = voice_bot
    audio = tmp_path/'sample.wav'
    audio.write_bytes(b'fake-audio')
    async def synthesize(*args,**kwargs):
        return audio
    entered = asyncio.Event()
    class TimeoutConnection(FakeConnection):
        async def send_group_msg(self,*args,**kwargs):
            entered.set()
            await asyncio.Event().wait()
    monkeypatch.setattr(module,'_request_tts',synthesize)
    ctx = context(bot,owner,TimeoutConnection())
    task = asyncio.create_task(execute(bot,owner,spec,ctx))
    try:
        await asyncio.wait_for(entered.wait(),1)
        assert bot.conversations.cancel(ctx.turn_id)
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(module._QUEUE.join(),1)
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert detail['tools'][0]['status'] == 'uncertain'
        row = await (await bot.storage.db.execute('SELECT status FROM delivery_parts')).fetchone()
        assert row['status'] == 'uncertain'
        assert await bot.storage.recovery_plan(ctx.turn_id) is None
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)


async def test_partial_voice_projects_only_first_acknowledged_segment(voice_bot,monkeypatch):
    bot,owner,spec,module = voice_bot
    async def synthesize(*args,**kwargs):
        return [b'first',b'second']
    monkeypatch.setattr(module,'_request_tts_batch',synthesize)
    class RejectSecondConnection(FakeConnection):
        async def send_group_msg(self,*args,**kwargs):
            if self.sent:
                raise ActionError('rejected',retcode=1400)
            return await super().send_group_msg(*args,**kwargs)
    ctx = context(bot,owner,RejectSecondConnection())
    ctx.turn_input = ctx.text
    async with bot.conversations.turn(ctx):
        output,silent = await bot.tool_executor.execute(ctx,spec.qualified_name,
            {'text':'第一段第二段','segments':['第一段','第二段']},{spec.qualified_name:(owner,spec)})
    assert json.loads(output)['error'] == 'tool_uncertain' and not silent
    rows = await (await bot.storage.db.execute(
        'SELECT status FROM delivery_parts ORDER BY rowid')).fetchall()
    assert [row['status'] for row in rows] == ['sent','failed']
    history = await bot.storage.recent_chat('group',ctx.group_id,self_id=ctx.self_id)
    assert [(item.role,item.content) for item in history] == [('user','念出来'),('assistant','第一段')]
    assert await bot.storage.recovery_plan(ctx.turn_id) is None


async def test_voice_queue_is_bounded_and_shutdown_discards_pending(voice_bot,monkeypatch):
    bot,owner,spec,module = voice_bot
    entered = asyncio.Event()
    async def synthesize(*args,**kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(module,'_request_tts',synthesize)
    first = module._enqueue(context(bot,owner),'第一条',None)
    await asyncio.wait_for(entered.wait(),1)
    pending = [module._enqueue(context(bot,owner,message_id=index+2),'排队',None) for index in range(32)]
    with pytest.raises(asyncio.QueueFull):
        module._enqueue(context(bot,owner,message_id=99),'队满',None)
    await asyncio.wait_for(bot.stop(),2)
    assert first.cancelled() and all(item.cancelled() for item in pending)
    assert module._QUEUE is None and module._WORKER is None
