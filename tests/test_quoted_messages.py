"""引用消息：原消息的正文与图片应进入本轮模型请求，且只在当前会话内读取。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot, private_message


def _reply_message(text: str = '猫猫 帮我总结一下', *, message_id: int = 1) -> dict:
    return group_message(text, message_id=message_id, message=[
        {'type': 'reply', 'data': {'id': '777'}},
        {'type': 'text', 'data': {'text': text}},
    ])


def _quoted(*, message_id: int = 777, group_id: int | None = 20002, message: list | None = None,
            sender: dict | None = None, timestamp: int = 1793720000) -> dict:
    payload = {
        'message_id': message_id,
        'message_type': 'group',
        'time': timestamp,
        'sender': sender or {'user_id': 40004, 'nickname': '衣食无忧', 'card': '', 'role': 'member'},
        'message': message if message is not None else [
            {'type': 'text', 'data': {'text': '深渊怎么能没有点危险东西呢'}},
            {'type': 'image', 'data': {'url': 'https://example.test/shot.png'}},
        ],
    }
    if group_id is not None:
        payload['group_id'] = group_id
    return payload


def _text_of(message: dict) -> str:
    content = message['content']
    if isinstance(content, str):
        return content
    return content[0]['text']


async def test_quoted_forward_expands_nested_text_images_and_caches_fetch(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, llm={'context_mode':'custom'}, overrides={
        'chat':{'allow_images':True,'read_forward':True,'send_time':False},
    })
    try:
        connection = FakeConnection()
        connection.get_msg_results[777] = _quoted(message=[{'type':'forward','data':{'id':'outer'}}])
        connection.forward_messages_by_id['outer'] = [{'sender':{'nickname':'转发者'},'message':[
            {'type':'text','data':{'text':'转发内的分析内容'}},
            {'type':'forward','data':{'id':'inner'}},
        ]}]
        connection.forward_messages_by_id['inner'] = [{'sender':{'nickname':'作者'},'message':[
            {'type':'text','data':{'text':'嵌套正文'}},
            {'type':'image','data':{'url':'https://example.test/nested.png'}},
        ]}]
        context = Context(bot=bot,connection=connection,event=parse_event(_reply_message()))
        messages = await bot.chat.build_messages('group',20002,context.text,ctx=context)
        text = _text_of(messages[-1])
        assert '转发内的分析内容' in text and '嵌套正文' in text and '[图片 1]' in text
        assert messages[-1]['content'][1]['image_url']['url'] == 'https://example.test/nested.png'
        await bot.chat.build_messages('group',20002,context.text,ctx=context)
        assert sum(action == 'get_forward_msg' for action, _ in connection.calls) == 2
    finally:
        await bot.stop()


@pytest.mark.parametrize('mode',['disabled','foreign','failed','images_off'])
async def test_quoted_forward_respects_reading_scope_and_switches(tmp_path,mode):
    bot = await make_bot(tmp_path,builtin=False,llm={'context_mode':'custom'},overrides={
        'chat':{'read_forward':mode != 'disabled','allow_images':mode != 'images_off'},
    })
    try:
        connection = FakeConnection()
        connection.get_msg_results[777] = _quoted(group_id=999 if mode == 'foreign' else 20002,
            message=[{'type':'forward','data':{'id':'forward'}}])
        connection.forward_messages = [{'message':[
            {'type':'text','data':{'text':'转发文字'}},
            {'type':'image','data':{'url':'https://example.test/private.png'}},
        ]}]
        if mode == 'failed':
            connection.forward_error = RuntimeError('unavailable')
        context = Context(bot=bot,connection=connection,event=parse_event(_reply_message()))
        messages = await bot.chat.build_messages('group',20002,context.text,ctx=context)
        text = _text_of(messages[-1])
        assert isinstance(messages[-1]['content'],str)
        assert 'https://example.test/private.png' not in text
        if mode in {'disabled','foreign'}:
            assert not any(action == 'get_forward_msg' for action,_ in connection.calls)
            assert '转发文字' not in text
        elif mode == 'failed':
            assert '读取失败' in text
        else:
            assert '转发文字' in text and '[图片]' in text
    finally:
        await bot.stop()


async def test_quoted_text_and_image_reach_the_model(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, llm={'context_mode': 'custom'}, overrides={
        'chat': {'allow_images': True, 'send_time': False},
    })
    try:
        connection = FakeConnection()
        connection.get_msg_results[777] = _quoted()
        ctx = Context(bot=bot, connection=connection, event=parse_event(_reply_message()))
        messages = await bot.chat.build_messages('group', 20002, ctx.text, ctx=ctx)

        content = messages[-1]['content']
        assert isinstance(content, list)
        text = content[0]['text']
        assert '【被引用消息】' in text
        assert '深渊怎么能没有点危险东西呢' in text
        assert '[图片 1]' in text
        assert '【当前消息】' in text and '帮我总结一下' in text
        assert content[1] == {'type': 'image_url', 'image_url': {'url': 'https://example.test/shot.png'}}
        # 模型必须知道“总结”指的是引用内容，而不是更早的群聊。
        assert '【被引用消息】' in messages[0]['content']
        assert ('get_msg', {'message_id': 777}) in connection.calls
    finally:
        await bot.stop()


async def test_quote_from_another_group_is_never_leaked(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, llm={'context_mode': 'custom'}, overrides={
        'chat': {'allow_images': True, 'send_time': False},
    })
    try:
        connection = FakeConnection()
        connection.get_msg_results[777] = _quoted(
            group_id=999, message=[{'type': 'text', 'data': {'text': '别的群里的秘密'}}],
        )
        ctx = Context(bot=bot, connection=connection, event=parse_event(_reply_message()))
        messages = await bot.chat.build_messages('group', 20002, ctx.text, ctx=ctx)

        text = _text_of(messages[-1])
        assert '不属于当前会话' in text
        assert '别的群里的秘密' not in text
        assert isinstance(messages[-1]['content'], str)
    finally:
        await bot.stop()


async def test_quoted_images_respect_the_image_switch(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, llm={'context_mode': 'custom'}, overrides={
        'chat': {'allow_images': False, 'send_time': False},
    })
    try:
        connection = FakeConnection()
        connection.get_msg_results[777] = _quoted()
        ctx = Context(bot=bot, connection=connection, event=parse_event(_reply_message()))
        messages = await bot.chat.build_messages('group', 20002, ctx.text, ctx=ctx)

        content = messages[-1]['content']
        assert isinstance(content, str)
        assert '深渊怎么能没有点危险东西呢' in content
        assert '[图片]' in content and '[图片 1]' not in content
        assert 'https://example.test/shot.png' not in content
    finally:
        await bot.stop()


async def test_message_without_reply_keeps_the_old_shape(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, llm={'context_mode': 'custom'}, overrides={
        'chat': {'allow_images': True, 'send_time': False},
    })
    try:
        connection = FakeConnection()
        ctx = Context(bot=bot, connection=connection, event=parse_event(group_message('普通消息')))
        messages = await bot.chat.build_messages('group', 20002, ctx.text, ctx=ctx)

        assert '【被引用消息】' not in _text_of(messages[-1])
        assert '【被引用消息】' not in messages[0]['content']
        assert 'get_msg' not in [action for action, _ in connection.calls]
    finally:
        await bot.stop()


async def test_unreadable_quote_degrades_to_a_placeholder(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, llm={'context_mode': 'custom'}, overrides={
        'chat': {'allow_images': True, 'send_time': False},
    })
    try:
        connection = FakeConnection()
        connection.get_msg_error = RuntimeError('协议端不支持 get_msg')
        ctx = Context(bot=bot, connection=connection, event=parse_event(_reply_message()))
        messages = await bot.chat.build_messages('group', 20002, ctx.text, ctx=ctx)

        text = _text_of(messages[-1])
        assert '【被引用消息】' in text
        assert '读取失败' in text
        assert '帮我总结一下' in text
    finally:
        await bot.stop()


async def test_private_quote_must_come_from_the_same_user(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, llm={'context_mode': 'custom'}, overrides={
        'chat': {'allow_images': True, 'send_time': False},
    })
    try:
        payload = private_message('总结一下')
        payload['message'] = [
            {'type': 'reply', 'data': {'id': '888'}},
            {'type': 'text', 'data': {'text': '总结一下'}},
        ]
        connection = FakeConnection()
        connection.get_msg_results[888] = {
            'message_id': 888, 'message_type': 'private', 'group_id': None,
            'sender': {'user_id': 99999, 'nickname': '别人'},
            'message': [{'type': 'text', 'data': {'text': '别人的私聊'}}],
        }
        ctx = Context(bot=bot, connection=connection, event=parse_event(payload))
        messages = await bot.chat.build_messages('private', 30003, ctx.text, ctx=ctx)
        assert '不属于当前会话' in _text_of(messages[-1])
        assert '别人的私聊' not in _text_of(messages[-1])

        connection.get_msg_results[888]['sender']['user_id'] = 30003
        reply = Context(bot=bot, connection=connection, event=parse_event(payload))
        messages = await bot.chat.build_messages('private', 30003, reply.text, ctx=reply)
        assert '别人的私聊' in _text_of(messages[-1])
    finally:
        await bot.stop()


async def test_agent_turn_hands_the_quoted_image_to_the_model(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm={
        'enabled': True, 'api_key': 'fake-test-key', 'context_mode': 'custom', 'context_window': 8192,
    }, overrides={'chat': {
        'allow_images': True, 'wake_word': '猫猫', 'send_time': False,
        'send_nickname': False, 'send_user_id': False, 'send_role': False,
    }})
    try:
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {
            'auto_group': True, 'trigger_modes': ['prefix'], 'cooldown_seconds': 0,
        }})
        complete = AsyncMock(return_value=SimpleNamespace(text='图里在聊深渊和田鼠', tool_calls=[]))
        monkeypatch.setattr(bot.chat, 'complete', complete)
        connection = FakeConnection()
        connection.get_msg_results[777] = _quoted()

        await bot.dispatcher.dispatch(parse_event(_reply_message()), connection)

        complete.assert_awaited_once()
        content = complete.call_args.args[0][-1]['content']
        assert isinstance(content, list)
        assert '深渊怎么能没有点危险东西呢' in content[0]['text']
        assert content[1] == {'type': 'image_url', 'image_url': {'url': 'https://example.test/shot.png'}}
        assert connection.texts() == ['图里在聊深渊和田鼠']
    finally:
        await bot.stop()
