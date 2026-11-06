import asyncio
import base64
import io
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from fllbot.onebot.connection import ActionError
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


@pytest.fixture
async def fallback(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': 'auto', 'forward_threshold': 600, 'reply_chunk_size': 500, 'reply_interval_ms': 0}})
    connection = FakeConnection()
    connection.send_group_forward_msg = AsyncMock(side_effect=ActionError('send_group_forward_msg', retcode=100))
    ctx = Context(bot, connection, parse_event(group_message('详细分析', message_id=99)))
    ctx.turn_input = ctx.text
    try:
        yield bot, connection, ctx
    finally:
        await bot.stop()


async def test_rejected_long_forward_sends_one_complete_image_instead_of_text_chunks(fallback):
    bot, connection, ctx = fallback
    text = '# 完整分析\n\n```python\nprint("中文")\n```\n\n' + '详细说明。' * 300
    async with bot.conversations.turn(ctx):
        await bot.deliveries.send_text(ctx, text)
    connection.send_group_forward_msg.assert_awaited_once()
    assert len(connection.sent) == 1
    segments = connection.sent[0][2]
    assert segments[0].type == 'reply' and segments[0].data['id'] == '99'
    assert [segment.type for segment in segments if segment.type != 'reply'] == ['image']
    image = base64.b64decode(segments[-1].data['file'].removeprefix('base64://'))
    with Image.open(io.BytesIO(image)) as picture:
        assert picture.format == 'PNG' and picture.height > 100 and picture.width == 900
    detail = await bot.storage.turn_detail(ctx.turn_id)
    assert detail['status'] == 'delivered'
    assert detail['deliveries'][0]['status'] == 'superseded'
    assert all(item['status'] == 'cancelled' for item in detail['deliveries'][1:-1])
    assert detail['deliveries'][-1]['status'] == 'sent'
    history = await bot.storage.recent_chat('group', 20002, self_id=ctx.self_id)
    assert [item.content for item in history if item.role == 'assistant'] == [text]
    assert await bot.storage.recovery_plan(ctx.turn_id) is None


async def test_render_failure_sends_only_one_short_notice(fallback):
    bot, connection, ctx = fallback
    bot.deliveries.image_renderer = Mock(side_effect=ValueError('font or layout unavailable'))
    text = 'LONG_BODY_MUST_NOT_FLOOD' * 500
    async with bot.conversations.turn(ctx):
        await bot.deliveries.send_text(ctx, text)
    assert len(connection.sent) == 1
    notice = ''.join(item.text_content for item in connection.sent[0][2] if item.is_text)
    assert len(notice) < 100 and 'LONG_BODY_MUST_NOT_FLOOD' not in notice
    detail = await bot.storage.turn_detail(ctx.turn_id)
    assert detail['status'] == 'failed'
    assert not any(item['status'] == 'pending' for item in detail['deliveries'])
    assert await bot.storage.recovery_plan(ctx.turn_id) is None


async def test_uncertain_forward_does_not_send_duplicate_image_or_text(fallback):
    bot, connection, ctx = fallback
    connection.send_group_forward_msg.side_effect = ActionError('send_group_forward_msg', message='timeout')
    bot.deliveries.image_renderer = Mock()
    with pytest.raises(ActionError):
        async with bot.conversations.turn(ctx):
            await bot.deliveries.send_text(ctx, 'uncertain content' * 100)
    bot.deliveries.image_renderer.assert_not_called()
    assert not connection.sent
    assert (await bot.storage.turn_detail(ctx.turn_id))['status'] == 'delivery_uncertain'


async def test_clear_during_image_render_blocks_late_send(fallback):
    bot, connection, ctx = fallback
    entered, release = threading.Event(), threading.Event()
    def render(text):
        entered.set()
        release.wait(timeout=3)
        return b'fake-image'
    bot.deliveries.image_renderer = render
    async def send():
        async with bot.conversations.turn(ctx):
            await bot.deliveries.send_text(ctx, 'long content' * 100)
    task = asyncio.create_task(send())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await bot.storage.clear_chat('group', 20002, self_id=ctx.self_id)
        release.set()
        await asyncio.wait_for(task, 3)
        assert not connection.sent
        assert (await bot.storage.turn_detail(ctx.turn_id))['status'] == 'cancelled'
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
