import base64
from unittest.mock import AsyncMock

import pytest

from fllbot.builtin_plugins.files.plugin import read_file
from fllbot.builtin_plugins.files.service import decode_text
from fllbot.onebot.models import parse_event
from fllbot.plugin import Context
from helpers import FakeConnection, group_message, make_bot


@pytest.mark.parametrize("quoted,foreign", [(False, False), (True, False), (True, True)])
async def test_attachment_is_bound_to_current_or_quoted_scene(tmp_path, quoted, foreign):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        file = {"type": "file", "data": {"name": "Example.cs", "file_id": "qq-file-id"}}
        payload = group_message("分析代码", message=[{"type": "reply", "data": {"id": "77"}}] if quoted else [file])

        async def call(action, params):
            if action == "get_msg":
                return {"group_id": 999 if foreign else 20002, "message": [file]}
            assert action == "get_file" and params == {"file_id": "qq-file-id"}
            return {"base64": base64.b64encode("class Example { /* 中文 */ }".encode()).decode()}

        connection = FakeConnection()
        connection.call = AsyncMock(side_effect=call)
        result = await read_file(Context(bot, connection, parse_event(payload)))
        if foreign:
            assert "error" in result and connection.call.await_count == 1
        else:
            assert "class Example" in result["content"] and result["next_offset"] is None
    finally:
        await bot.stop()


def test_text_encodings_and_binary_limits():
    for encoding in ["utf-8-sig", "utf-16", "gb18030"]:
        assert decode_text("中文代码".encode(encoding))[0] == "中文代码"
    with pytest.raises(ValueError):
        decode_text(b"\x00binary")
    with pytest.raises(ValueError):
        decode_text(b"a" * 1048577)


async def test_attachment_read_has_pagination(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        connection = FakeConnection()
        connection.call = AsyncMock(return_value={"base64": base64.b64encode(b"a" * 13000).decode()})
        payload = group_message("read", message=[{"type": "file", "data": {"name": "source.cs", "file_id": "id"}}])
        ctx = Context(bot, connection, parse_event(payload))
        first = await read_file(ctx)
        second = await read_file(ctx, offset=first["next_offset"])
        assert len(first["content"]) == 12000 and len(second["content"]) == 1000
        assert second["next_offset"] is None
    finally:
        await bot.stop()
