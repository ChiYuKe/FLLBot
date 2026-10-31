from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from fllbot.llm.output import send_chat_reply
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot, private_message


@pytest.mark.parametrize('private', [False, True])
async def test_long_replies_use_forward_nodes(tmp_path: Path, private):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'forward_threshold': 200, 'reply_chunk_size': 100, 'reply_interval_ms': 0,
    }})
    try:
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock()
        connection.send_private_forward_msg = AsyncMock()
        payload = private_message('你好') if private else group_message('你好')
        ctx = Context(bot=bot, connection=connection, event=parse_event(payload))
        text = '长回复内容' * 80
        await send_chat_reply(ctx, text)
        target = connection.send_private_forward_msg if private else connection.send_group_forward_msg
        target.assert_awaited_once()
        nodes = target.call_args.args[1]
        assert all(node['type'] == 'node' for node in nodes)
        assert all(node['data']['user_id'] == str(ctx.self_id) for node in nodes)
        assert ''.join(n['data']['content'][0]['data']['text'] for n in nodes) == text
        assert not connection.sent
    finally:
        await bot.stop()


async def test_forward_failure_falls_back_without_losing_reply(tmp_path: Path):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': 'forward', 'reply_chunk_size': 50, 'reply_interval_ms': 0,
        'quote_reply': True, 'at_sender': True,
    }})
    try:
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock(side_effect=RuntimeError('unsupported'))
        ctx = Context(bot=bot, connection=connection, event=parse_event(group_message('你好')))
        text = '测试回复' * 50
        await send_chat_reply(ctx, text)
        sent = [segment for _, _, segments in connection.sent for segment in segments]
        assert sum(segment.type == 'reply' for segment in sent) == 1
        assert sum(segment.type == 'at' for segment in sent) == 1
        assert ''.join(segment.text_content for segment in sent if segment.is_text).replace(' ', '') == text
    finally:
        await bot.stop()


@pytest.mark.parametrize('mode,length,expected', [('auto', 20, False), ('split', 300, False), ('forward', 20, True)])
async def test_short_and_explicit_output_modes(tmp_path: Path, mode, length, expected):
    bot = await make_bot(tmp_path, builtin=False, overrides={'chat': {
        'reply_mode': mode, 'forward_threshold': 200, 'reply_interval_ms': 0,
    }})
    try:
        connection = FakeConnection()
        connection.send_group_forward_msg = AsyncMock()
        ctx = Context(bot=bot, connection=connection, event=parse_event(group_message('你好')))
        await send_chat_reply(ctx, '文' * length)
        assert bool(connection.send_group_forward_msg.await_count) is expected
        assert bool(connection.sent) is not expected
    finally:
        await bot.stop()
