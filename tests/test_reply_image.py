from unittest.mock import AsyncMock, Mock

import pytest

from fllbot.config import ChatConfig
from fllbot.llm.output import send_chat_reply
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


def test_auto_image_is_the_chat_default():
    assert ChatConfig().reply_mode == 'auto_image'


async def test_image_mode_sends_one_picture_with_quote_and_keeps_memory(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': 'image', 'reply_interval_ms': 0, 'quote_reply': True, 'at_sender': True}})
    try:
        bot.deliveries.image_renderer = lambda text: b'png-image'
        connection = FakeConnection()
        ctx = Context(bot, connection, parse_event(group_message('你好', message_id=99)))
        text = '测试回复' * 50
        async with bot.conversations.turn(ctx):
            await send_chat_reply(ctx, text)
        assert len(connection.sent) == 1
        segments = connection.sent[0][2]
        assert segments[0].type == 'reply' and segments[0].data['id'] == '99'
        assert segments[1].type == 'at' and segments[1].data['qq'] == str(ctx.user_id)
        assert [segment.type for segment in segments if segment.type not in {'reply', 'at', 'text'}] == ['image']
        assert not ''.join(segment.text_content for segment in segments if segment.is_text).strip()
        detail = await bot.storage.turn_detail(ctx.turn_id)
        assert detail['status'] == 'delivered'
        history = await bot.storage.recent_chat('group', ctx.group_id, self_id=ctx.self_id)
        assert [item.content for item in history if item.role == 'assistant'] == [text]
    finally:
        await bot.stop()


@pytest.mark.parametrize('mode,length,expect_image', [
    ('auto_image', 20, False),
    ('auto_image', 300, True),
    ('image', 20, True),
])
async def test_image_modes_respect_threshold(tmp_path, mode, length, expect_image):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': mode, 'forward_threshold': 200, 'reply_interval_ms': 0}})
    try:
        render = Mock(return_value=b'png-image')
        bot.deliveries.image_renderer = render
        connection = FakeConnection()
        ctx = Context(bot, connection, parse_event(group_message('你好')))
        async with bot.conversations.turn(ctx):
            await send_chat_reply(ctx, '文' * length)
        images = [segment for _, _, segments in connection.sent for segment in segments if segment.type == 'image']
        assert bool(images) is expect_image
        assert render.called is expect_image
        if expect_image:
            assert [segment.type for segment in connection.sent[0][2]] == ['image']
    finally:
        await bot.stop()


async def test_render_failure_falls_back_to_forward_without_losing_text(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': 'image', 'forward_threshold': 200, 'reply_chunk_size': 50, 'reply_interval_ms': 0}})
    try:
        bot.deliveries.image_renderer = Mock(side_effect=ValueError('font unavailable'))
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock(return_value={'message_id': 'forward-1'})
        ctx = Context(bot, connection, parse_event(group_message('你好')))
        text = '长回复内容' * 80
        async with bot.conversations.turn(ctx):
            await send_chat_reply(ctx, text)
        connection.send_group_forward_msg.assert_awaited_once()
        assert not connection.sent
        history = await bot.storage.recent_chat('group', ctx.group_id, self_id=ctx.self_id)
        assert [item.content for item in history if item.role == 'assistant'] == [text]
        assert (await bot.storage.turn_detail(ctx.turn_id))['status'] == 'delivered'
    finally:
        await bot.stop()
