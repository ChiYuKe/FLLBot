from unittest.mock import AsyncMock, Mock

import pytest

from fllbot.builtin_plugins.chat_dispatch.plugin import ChatDispatch
from fllbot.config import ChatConfig
from fllbot.delivery_policy import requests_text_only
from fllbot.llm.output import send_chat_reply
from fllbot.onebot.connection import ActionError
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot

_TABLE_REPLY = (
    "## 版本对照\n\n"
    "| 版本 | 特点 | 步数 |\n"
    "| --- | :--- | ---: |\n"
    "| dev | 开放权重，可本地运行 | 20～28 |\n"
    "| schnell | 蒸馏版 | 1～4 |\n\n"
    "本地首选 dev。\n"
)


def test_table_detection_uses_markdown_structure():
    dispatch = ChatDispatch()
    [(start, end)] = dispatch.markdown_tables(_TABLE_REPLY)
    assert _TABLE_REPLY[start:end].startswith("| 版本")
    assert "本地首选" not in _TABLE_REPLY[start:end]
    assert not dispatch.markdown_tables("普通竖线 A | B --- C")
    assert not dispatch.markdown_tables(f"```markdown\n{_TABLE_REPLY}```")


@pytest.mark.parametrize("model_mode", ["split", None])
async def test_auto_dispatch_renders_unsplit_markdown_table(tmp_path, model_mode):
    bot = await make_bot(tmp_path, overrides={"chat": {
        "reply_mode": "auto", "reply_chunk_size": 500, "reply_interval_ms": 0,
    }})
    try:
        render = Mock(wraps=bot.deliveries.image_renderer)
        bot.deliveries.image_renderer = render
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock()
        ctx = Context(bot, connection, parse_event(group_message("比较这两个版本")))
        ctx.turn_input = "比较这两个版本"
        async with bot.conversations.turn(ctx):
            await send_chat_reply(ctx, _TABLE_REPLY, model_reply_mode=model_mode)

        render.assert_called_once_with(_TABLE_REPLY, markdown_only=True)
        connection.send_group_forward_msg.assert_not_awaited()
        assert len(connection.sent) == 1
        assert [segment.type for segment in connection.sent[0][2]] == ["reply", "image"]
        history = await bot.storage.recent_chat("group", ctx.group_id, self_id=ctx.self_id)
        assert [item.content for item in history if item.role == "assistant"] == [_TABLE_REPLY]
    finally:
        await bot.stop()


@pytest.mark.parametrize("request_text, model_mode", [
    ("比较这两个版本", "forward"),
    ("比较这两个版本，发成聊天记录", "split"),
    ("比较这两个版本", "split"),
])
async def test_forward_can_mix_table_images_with_text(tmp_path, request_text, model_mode):
    bot = await make_bot(tmp_path, overrides={"chat": {
        "reply_mode": "auto", "reply_chunk_size": 50, "reply_interval_ms": 0,
    }})
    try:
        render = Mock(wraps=bot.deliveries.image_renderer)
        bot.deliveries.image_renderer = render
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock(return_value={"message_id": 42})
        ctx = Context(bot, connection, parse_event(group_message(request_text)))
        ctx.turn_input = request_text
        async with bot.conversations.turn(ctx):
            await send_chat_reply(ctx, _TABLE_REPLY, model_reply_mode=model_mode)

        connection.send_group_forward_msg.assert_awaited_once()
        assert not connection.sent
        [(start, end)] = ChatDispatch().markdown_tables(_TABLE_REPLY)
        render.assert_called_once_with(_TABLE_REPLY[start:end], markdown_only=True)
        contents = [node["data"]["content"][-1] for node in connection.send_group_forward_msg.await_args.args[1]]
        assert [part["type"] for part in contents] == ["text", "image", "text"]
        assert contents[0]["data"]["text"].strip() == "## 版本对照"
        assert contents[1]["data"]["file"].startswith("base64://iVBOR")
        assert contents[2]["data"]["text"].strip() == "本地首选 dev。"
        history = await bot.storage.recent_chat("group", ctx.group_id, self_id=ctx.self_id)
        assert [item.content for item in history if item.role == "assistant"] == [_TABLE_REPLY]
    finally:
        await bot.stop()


@pytest.mark.parametrize("request_text, renderer_fails, expect_forward", [
    ("比较版本，用文字回答", False, False),
    ("比较版本，给我 Markdown 源码", False, True),
    ("比较版本，发成聊天记录", True, True),
    ("比较版本", True, True),
])
async def test_table_follows_user_preference_or_falls_back_to_original_plan(
    tmp_path, request_text, renderer_fails, expect_forward,
):
    bot = await make_bot(tmp_path, overrides={"chat": {"reply_mode": "auto", "reply_interval_ms": 0}})
    try:
        render = Mock(side_effect=ValueError("layout unavailable"))
        bot.deliveries.image_renderer = render
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock(return_value={"message_id": 42})
        ctx = Context(bot, connection, parse_event(group_message(request_text)))
        ctx.turn_input = request_text
        async with bot.conversations.turn(ctx):
            await send_chat_reply(ctx, _TABLE_REPLY, model_reply_mode="forward")

        assert render.called is renderer_fails
        assert bool(connection.send_group_forward_msg.await_count) is expect_forward
        if expect_forward:
            nodes = connection.send_group_forward_msg.await_args.args[1]
            rendered = "".join(node["data"]["content"][-1]["data"]["text"] for node in nodes)
            assert rendered.strip() == _TABLE_REPLY.strip()
        else:
            assert connection.sent[0][2][-1].text_content == _TABLE_REPLY
    finally:
        await bot.stop()


def test_auto_image_is_the_chat_default():
    assert ChatConfig().reply_mode == 'auto_image'


@pytest.mark.parametrize('request_text, expected', [
    ('给我一份可以直接复制的', True),
    ('不要转图片，直接发可复制文本', True),
    ('别把这段内容渲染成图片', True),
    ('给我一份纯文本', True),
    ('提示词要能直接复制粘贴', True),
    ('给我一份图片', False),
    ('把这段内容转成图片', False),
    ('怎么把提示词做成图片', False),
])
def test_detects_copyable_text_requests(request_text, expected):
    assert requests_text_only(request_text) is expected


@pytest.mark.parametrize('mode', ['auto_image', 'image', 'auto', 'forward'])
async def test_direct_text_reply_overrides_delivery_mode_and_preserves_whitespace(tmp_path, mode):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': mode, 'forward_threshold': 200, 'reply_chunk_size': 100, 'reply_interval_ms': 0}})
    try:
        render = Mock(return_value=b'png-image')
        bot.deliveries.image_renderer = render
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock()
        request_text = '不要转图片，直接发可复制文本'
        ctx = Context(bot, connection, parse_event(group_message(request_text)))
        ctx.turn_input = request_text
        answer = ("正向：\n    clean lineart, soft shadows\n\n" * 9) + "负向：bad anatomy\n"
        async with bot.conversations.turn(ctx):
            await send_chat_reply(ctx, answer)
        render.assert_not_called()
        connection.send_group_forward_msg.assert_not_awaited()
        chunks = [segments[-1] for _, _, segments in connection.sent]
        assert len(chunks) > 1 and all(chunk.is_text for chunk in chunks)
        assert ''.join(chunk.text_content for chunk in chunks) == answer
    finally:
        await bot.stop()


@pytest.mark.parametrize("model_reply_mode", ["split", "forward"])
async def test_model_forward_rejection_falls_back_to_copyable_messages(tmp_path, model_reply_mode):
    bot = await make_bot(tmp_path, overrides={'chat': {
        'reply_mode': 'auto_image', 'reply_chunk_size': 50, 'reply_interval_ms': 0}})
    try:
        bot.deliveries.image_renderer = Mock(return_value=b'png-image')
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock(side_effect=ActionError("send_group_forward_msg", retcode=100))
        request_text = '给我一份可以直接复制的'
        ctx = Context(bot, connection, parse_event(group_message(request_text)))
        ctx.turn_input = request_text
        answer = "保留缩进：\n    clean lineart, soft shadows\n\n" * 7
        async with bot.conversations.turn(ctx):
            await send_chat_reply(ctx, answer, model_reply_mode=model_reply_mode)

        connection.send_group_forward_msg.assert_awaited_once()
        bot.deliveries.image_renderer.assert_not_called()
        assert len(connection.sent) > 1
        assert ''.join(segments[-1].text_content for _, _, segments in connection.sent) == answer
        assert (await bot.storage.turn_detail(ctx.turn_id))["status"] == "delivered"
    finally:
        await bot.stop()


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
