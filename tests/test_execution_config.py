import asyncio
import json
from unittest.mock import AsyncMock

from fllbot.llm.chat import ChatReply
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


async def test_waiting_turn_freezes_agent_choices_but_execution_checks_live_grants(tmp_path,monkeypatch):
    from fllbot.builtin_plugins.agent.plugin import _get_tools, _run_tool
    bot = await make_bot(tmp_path,llm={'enabled':True,'api_key':'fake','context_window':8192})
    await bot.permissions.save_group(20002,enabled=True,config={'policy':{
        'auto_group':True,'trigger_modes':['at'],'max_tool_rounds':1,'allowed_tools':['basic__check_online']}})
    ctx = Context(bot,FakeConnection(),parse_event(group_message('hello',at=10001)),plugin=bot.plugins.get('agent'))
    ctx.turn_input = ctx.text
    entered = asyncio.Event()
    original = bot.storage.create_turn
    callback = AsyncMock(return_value={'online':True})
    monkeypatch.setattr(bot.plugins.get('basic').ai_tools[0],'callback',callback)
    async def create(*args):
        result = await original(*args)
        entered.set()
        return result
    monkeypatch.setattr(bot.storage,'create_turn',create)
    captured = {}
    async def run():
        async with bot.conversations.turn(ctx):
            captured['settings'] = bot.chat.turn_agent_settings(20002,10001)
            definitions,registry = _get_tools(ctx)
            captured['names'] = [item['function']['name'] for item in definitions]
            await bot.chat.build_messages('group',20002,ctx.text,ctx=ctx,tools=definitions)
            captured['version'] = ctx.context_snapshot.config_version
            output,_ = await _run_tool(ctx,'basic__check_online',{},registry)
            assert json.loads(output)['error'] == 'tool_permission_denied'
    try:
        async with bot.chat.conversation_lock(10001,'group',20002,ctx.user_id):
            task = asyncio.create_task(run())
            await asyncio.wait_for(entered.wait(),1)
            await bot.permissions.save_group(20002,enabled=True,config={'policy':{
                'auto_group':True,'trigger_modes':['at'],'max_tool_rounds':5,'allowed_tools':[]}})
        await task
        assert captured['settings']['allowed_tools'] == ['basic__check_online']
        assert captured['names'] == ['basic__check_online']
        assert (await bot.storage.turn_detail(ctx.turn_id))['config_version'] == captured['version']
        callback.assert_not_awaited()
        fresh = Context(bot,FakeConnection(),parse_event(group_message('hello',at=10001,message_id=2)))
        async with bot.conversations.turn(fresh):
            assert bot.chat.turn_agent_settings(20002,10001)['allowed_tools'] == []
            assert bot.chat.execution_version != captured['version']
    finally:
        await bot.stop()


async def test_model_context_uses_frozen_config_but_authorization_is_live(tmp_path,monkeypatch):
    bot = await make_bot(tmp_path,builtin=False,llm={'enabled':True,'api_key':'fake','model':'old-model','persona':'snapshot','context_mode':'model'})
    bot.personas.save('snapshot','snapshot','','old persona')
    entered, release = asyncio.Event(),asyncio.Event()
    captured = {}
    async def window(**kwargs):
        entered.set()
        await release.wait()
        return 8192
    async def complete(messages,**kwargs):
        captured['messages'] = messages
        captured['model'] = bot.chat.client.config.model
        captured['max_tokens'] = bot.chat.client.config.max_tokens
        captured['allowed'] = bot.chat.scene_enabled('group',20002)
        return ChatReply('answer','snapshot','old-model')
    monkeypatch.setattr(bot.chat.client,'context_window',window)
    monkeypatch.setattr(bot.chat,'complete',complete)
    ctx = Context(bot,FakeConnection(),parse_event(group_message('hello')))
    task = asyncio.create_task(bot.chat.generate('group',20002,'hello',ctx=ctx))
    try:
        await asyncio.wait_for(entered.wait(),1)
        old_tokens = bot.config.llm.max_tokens
        await bot.update_config({'llm':{'model':'new-model','max_tokens':1234,'enable_group':False}})
        bot.personas.save('snapshot','snapshot','','new persona')
        release.set()
        await task
        assert captured['model'] == 'old-model'
        assert captured['max_tokens'] == old_tokens
        system = captured['messages'][0]['content']
        assert system.startswith('old persona')
        assert 'new persona' not in system
        assert '本轮任务边界' in system
        assert captured['allowed'] is False
        assert bot.chat.client.config.model == 'new-model'
        assert bot.chat.execution_config is bot.config
        assert bot.chat.system_prompt('group',20002,ctx) == 'new persona'
    finally:
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        await bot.stop()


async def test_turn_freezes_persona_before_waiting_for_conversation_lock(tmp_path):
    bot = await make_bot(tmp_path,builtin=False,llm={'persona':'snapshot'})
    bot.personas.save('snapshot','snapshot','','before queue')
    ctx = Context(bot,FakeConnection(),parse_event(group_message('hello')))
    lock = bot.chat.conversation_lock(10001,'group',20002,ctx.user_id)
    entered = asyncio.Event()
    original = bot.storage.create_turn
    async def create(*args):
        result = await original(*args)
        entered.set()
        return result
    bot.storage.create_turn = create
    result = []
    async def run():
        async with bot.conversations.turn(ctx):
            result.append(bot.chat.system_prompt('group',20002,ctx))
    try:
        async with lock:
            task = asyncio.create_task(run())
            await asyncio.wait_for(entered.wait(),1)
            # Allow the lifecycle to enter its immutable scope and wait for the held lock.
            await asyncio.sleep(0)
            bot.personas.save('snapshot','snapshot','','after queue')
        await task
        assert result == ['before queue']
    finally:
        await bot.stop()
