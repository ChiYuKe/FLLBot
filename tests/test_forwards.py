from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.llm.forward import MAX_FETCHES, forward_transcript, has_forward
from fllbot.onebot.models import parse_event
from fllbot.onebot.segments import normalize_message
from helpers import FakeConnection, group_message, make_bot


class ForwardConnection:
    """按 id 返回不同合并转发内容的假连接。"""

    def __init__(self, data: dict[str, list[dict]]) -> None:
        self.data = data
        self.requested: list[str] = []

    async def get_forward_msg(self, message_id: str):
        self.requested.append(message_id)
        return {"messages": self.data.get(message_id, [])}


def forward_segments(forward_id: str):
    return normalize_message([{"type": "forward", "data": {"id": forward_id}}])


def test_has_forward() -> None:
    assert has_forward(forward_segments("x"))
    assert not has_forward(normalize_message([{"type": "text", "data": {"text": "x"}}]))


async def test_forward_transcript_renders_text_images_and_nested_records() -> None:
    connection = ForwardConnection({
        "outer": [
            {"sender": {"nickname": "keewar"}, "message": [{"type": "text", "data": {"text": "外层"}}]},
            {"sender": {"nickname": "阿甲"}, "message": [{"type": "forward", "data": {"id": "inner"}}]},
        ],
        "inner": [
            {"sender": {"nickname": "北溟南渊"}, "message": [{"type": "text", "data": {"text": "里层一"}}]},
            {"sender": {"card": "Azusa"}, "message": [
                {"type": "text", "data": {"text": "里层二"}},
                {"type": "image", "data": {"file": "https://example.test/a.png"}},
            ]},
        ],
    })
    text = await forward_transcript(connection, forward_segments("outer"))
    assert text.startswith("【合并转发·共2条】")
    assert "keewar：外层" in text
    assert "阿甲：\n【合并转发·共2条】" in text
    assert "北溟南渊：里层一" in text
    assert "Azusa：里层二[图片]" in text
    assert connection.requested == ["outer", "inner"]


async def test_forward_transcript_accepts_wrapped_nodes() -> None:
    connection = ForwardConnection({"fid": [
        {"type": "node", "data": {"sender": {"nickname": "甲"}, "message": [{"type": "text", "data": {"text": "包装节点"}}]}},
    ]})
    text = await forward_transcript(connection, forward_segments("fid"))
    assert "甲：包装节点" in text


async def test_forward_transcript_stops_on_cycles_and_errors() -> None:
    looped = ForwardConnection({"same": [
        {"sender": {"nickname": "甲"}, "message": [{"type": "text", "data": {"text": "再转"}}]},
        {"sender": {"nickname": "乙"}, "message": [{"type": "forward", "data": {"id": "same"}}]},
    ]})
    text = await forward_transcript(looped, forward_segments("same"))
    assert looped.requested == ["same"]
    assert "乙：[合并转发]" in text

    failing = FakeConnection()
    failing.forward_error = RuntimeError("unsupported")
    assert await forward_transcript(failing, forward_segments("bad")) == ""


async def test_forward_transcript_is_truncated() -> None:
    nodes = [{"sender": {"nickname": f"用户{i}"}, "message": [{"type": "text", "data": {"text": "内容" * 40}}]} for i in range(60)]
    text = await forward_transcript(ForwardConnection({"big": nodes}), forward_segments("big"), limit=300)
    assert len(text) <= 300
    assert text.endswith("…")


async def test_forward_transcript_caps_nested_fetches() -> None:
    data = {
        "outer": [
            {"sender": {"nickname": f"甲{i}"}, "message": [{"type": "forward", "data": {"id": f"inner{i}"}}]}
            for i in range(10)
        ],
    }
    data.update({
        f"inner{i}": [{"sender": {"nickname": "乙"}, "message": [{"type": "text", "data": {"text": "内容"}}]}]
        for i in range(10)
    })
    connection = ForwardConnection(data)
    await forward_transcript(connection, forward_segments("outer"))
    assert connection.requested[0] == "outer"
    assert len(connection.requested) == MAX_FETCHES


@pytest.mark.parametrize("enabled", [False, True])
async def test_agent_reads_forward_only_when_enabled(tmp_path: Path, monkeypatch, enabled: bool) -> None:
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake-test-key", "context_mode": "custom"}, overrides={"chat": {
        "send_nickname": False, "send_user_id": False, "send_role": False, "read_forward": enabled,
    }})
    try:
        await bot.permissions.save_group(20002, enabled=True, config={"policy": {
            "auto_group": True, "trigger_modes": ["all"], "cooldown_seconds": 0,
        }})
        complete = AsyncMock(return_value=SimpleNamespace(text="看到了", tool_calls=[]))
        monkeypatch.setattr(bot.chat, "complete", complete)
        connection = FakeConnection()
        connection.forward_messages = [
            {"sender": {"nickname": "甲"}, "message": [{"type": "text", "data": {"text": "转发正文"}}]},
        ]
        payload = group_message("看看这个")
        payload["message"].append({"type": "forward", "data": {"id": "fid"}})
        await bot.dispatcher.dispatch(parse_event(payload), connection)
        content = complete.call_args.args[0][-1]["content"]
        if enabled:
            assert "转发正文" in content
            assert "【合并转发·共1条】" in content
            rows = await bot.storage.recent_chat("group", 20002)
            assert "转发正文" in rows[0].content
        else:
            assert "转发正文" not in content
            assert all(name != "get_forward_msg" for name, _ in connection.calls)
    finally:
        await bot.stop()


async def test_forward_only_message_replaces_placeholder(tmp_path: Path, monkeypatch) -> None:
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake-test-key", "context_mode": "custom"}, overrides={"chat": {
        "send_nickname": False, "send_user_id": False, "send_role": False, "read_forward": True,
    }})
    try:
        await bot.permissions.save_group(20002, enabled=True, config={"policy": {
            "auto_group": True, "trigger_modes": ["all"], "cooldown_seconds": 0,
        }})
        complete = AsyncMock(return_value=SimpleNamespace(text="看到了", tool_calls=[]))
        monkeypatch.setattr(bot.chat, "complete", complete)
        connection = FakeConnection()
        connection.forward_messages = [
            {"sender": {"nickname": "甲"}, "message": [{"type": "text", "data": {"text": "只有转发"}}]},
        ]
        # 只发合并转发、不带正文。
        payload = group_message("")
        payload["message"].append({"type": "forward", "data": {"id": "fid"}})
        await bot.dispatcher.dispatch(parse_event(payload), connection)
        content = complete.call_args.args[0][-1]["content"]
        assert content == "【合并转发·共1条】\n甲：只有转发"
    finally:
        await bot.stop()
