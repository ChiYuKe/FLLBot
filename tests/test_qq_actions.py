"""QQ API 参数与 echo 响应链路回归。"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.connection import ActionError, OneBotConnection
from fllbot.onebot.models import MessageEvent
from fllbot.onebot.segments import MessageSegment, normalize_message
from fllbot.plugin.context import Context
from helpers import FakeConnection


class EchoSocket:
    async def send_text(self, text):
        self.payload = json.loads(text)
        await self.connection._handle_payload({
            "echo": self.payload["echo"], "status": self.status,
            "retcode": 0 if self.status == "ok" else 100,
            "data": {"result": "received"}, "wording": "permission denied",
        })


def connection(status="ok"):
    socket = EchoSocket()
    socket.status = status
    conn = OneBotConnection(socket, on_event=AsyncMock())
    socket.connection = conn
    return conn, socket


@pytest.mark.parametrize(("method", "args", "kwargs", "action", "params"), [
    ("get_group_member_list", (123,), {"no_cache": True}, "get_group_member_list",
     {"group_id": 123, "no_cache": True}),
    ("send_poke", (456,), {"group_id": 123}, "group_poke", {"group_id": 123, "user_id": 456}),
    ("send_poke", (456,), {}, "friend_poke", {"user_id": 456}),
    ("send_group_notice", (123, "公告"), {}, "_send_group_notice", {"group_id": 123, "content": "公告"}),
    ("send_group_notice", (123, "公告"), {"image": "https://example.com/a.png"}, "_send_group_notice",
     {"group_id": 123, "content": "公告", "image": "https://example.com/a.png"}),
    ("get_group_notice", (123,), {}, "_get_group_notice", {"group_id": 123}),
    ("delete_group_notice", (123, "n1"), {}, "_del_group_notice", {"group_id": 123, "notice_id": "n1"}),
    ("set_group_special_title", (123, 456, "头衔"), {}, "set_group_special_title",
     {"group_id": 123, "user_id": 456, "special_title": "头衔"}),
    ("set_essence_msg", (789,), {}, "set_essence_msg", {"message_id": 789}),
    ("send_like", (456, 5), {}, "send_like", {"user_id": 456, "times": 5}),
    ("send_friend_request", (456,), {"message": "你好"}, "send_friend_request",
     {"user_id": 456, "message": "你好"}),
    ("set_friend_add_request", ("friend-flag",), {"approve": True}, "set_friend_add_request",
     {"flag": "friend-flag", "approve": True, "remark": ""}),
    ("upload_group_file", (123, "file:///a.txt", "a.txt"), {}, "upload_group_file",
     {"group_id": 123, "file": "file:///a.txt", "name": "a.txt"}),
    ("upload_group_file", (123, "a.txt", "a.txt"), {"folder": "/abc"}, "upload_group_file",
     {"group_id": 123, "file": "a.txt", "name": "a.txt", "folder": "/abc"}),
    ("get_group_file_url", (123, "fid", 102), {}, "get_group_file_url",
     {"group_id": 123, "file_id": "fid", "busid": 102}),
    ("rename_group_file", (123, "fid", "/", "b.txt"), {}, "rename_group_file",
     {"group_id": 123, "file_id": "fid", "current_parent_directory": "/", "new_name": "b.txt"}),
    ("get_record", ("voice",), {}, "get_record", {"file": "voice", "out_format": "mp3"}),
    ("can_send_record", (), {}, "can_send_record", {}),
])
async def test_action_wire(method, args, kwargs, action, params):
    conn, socket = connection()
    result = await getattr(conn, method)(*args, **kwargs)
    assert result == {"result": "received"}
    assert socket.payload == {"action": action, "params": params, "echo": socket.payload["echo"]}
    assert not conn._pending


async def test_action_permission_failure():
    conn, _ = connection("failed")
    with pytest.raises(ActionError, match="permission denied"):
        await conn.set_group_admin(123, 456)
    assert not conn._pending


@pytest.mark.parametrize("receipt", [
    {}, {"status": "ok"}, {"status": "ok", "retcode": False}, {"status": "async", "retcode": 1},
])
async def test_friend_request_requires_a_definite_receipt(receipt):
    conn, socket = connection()

    async def send_text(text):
        payload = json.loads(text)
        await conn._handle_payload({"echo": payload["echo"], **receipt})

    socket.send_text = send_text
    with pytest.raises(ActionError, match="好友申请缺少明确的成功回执"):
        await conn.send_friend_request(456, message="你好")
    assert not conn._pending


@pytest.mark.parametrize(("message_type", "sub_type", "entrypoint"), [
    ("private", "group", "qq"),
    ("group", "anonymous", "qq"),
    ("group", "normal", "admin_debug"),
])
async def test_friend_request_requires_a_real_group_sender(message_type, sub_type, entrypoint):
    conn, _ = connection()
    conn.send_friend_request = AsyncMock()
    event = MessageEvent(
        self_id=10001, user_id=456, group_id=123, message_id=10,
        message_type=message_type, sub_type=sub_type, message="加我好友",
    )
    ctx = Context(bot=SimpleNamespace(), connection=conn, event=event, entrypoint=entrypoint)
    assert (await ctx.request_friendship())["status"] == "ineligible"
    conn.send_friend_request.assert_not_awaited()


async def test_auto_approve_incoming_friend_requests(bot, monkeypatch):
    first = FakeConnection()
    second = FakeConnection(10002)
    complete = AsyncMock()
    monkeypatch.setattr(bot.chat, "complete", complete)
    plugin = bot.plugins.get("chat_dispatch")
    request = {"post_type": "request", "request_type": "friend", "user_id": 456, "flag": "friend-flag"}

    await bot.on_raw_event(first, {**request, "flag": "disabled-flag"})
    await bot.dispatcher._queue.join()
    assert first.calls == []

    await plugin.settings.set("auto_approve_friend_requests", True)
    await bot.on_raw_event(first, request)
    await bot.on_raw_event(first, {**request, "self_id": first.self_id, "time": 123})
    await bot.on_raw_event(second, {**request, "self_id": second.self_id})
    await bot.on_raw_event(first, {**request, "request_type": "group", "sub_type": "invite", "group_id": 123})
    await bot.on_raw_event(first, {**request, "flag": " "})
    await bot.on_raw_event(first, {**request, "flag": "self-request", "user_id": first.self_id})
    await bot.on_raw_event(first, {**request, "flag": "wrong-account", "self_id": second.self_id})
    await bot.dispatcher._queue.join()

    approval = ("set_friend_add_request", {"flag": "friend-flag", "approve": True, "remark": ""})
    assert first.calls == second.calls == [approval]
    complete.assert_not_awaited()

    await plugin.settings.set("auto_approve_friend_requests", False)
    await bot.on_raw_event(first, {**request, "flag": "switched-off"})
    await bot.dispatcher._queue.join()
    assert first.calls == [approval]


@pytest.mark.parametrize("group_id", [None, 123])
async def test_context_poke_and_members(group_id):
    conn, socket = connection()
    event = MessageEvent(self_id=1, user_id=456, group_id=group_id,
                         message_type="group" if group_id else "private")
    ctx = Context(bot=SimpleNamespace(), connection=conn, event=event)
    await ctx.poke()
    assert socket.payload["action"] == ("group_poke" if group_id else "friend_poke")
    assert socket.payload["params"]["user_id"] == 456
    if group_id:
        await ctx.get_members(no_cache=True)
        assert socket.payload["params"] == {"group_id": 123, "no_cache": True}
    else:
        with pytest.raises(ValueError):
            await ctx.get_members()


def test_media_and_forward_payload():
    segments = [MessageSegment.record("base64://abc"), MessageSegment.video("file:///a.mp4"),
                MessageSegment.file("file:///a.txt", name="资料.txt"),
                MessageSegment.xml_card("<msg/>"), MessageSegment.dice(), MessageSegment.rps()]
    assert [s.to_dict() for s in normalize_message([s.to_dict() for s in segments])] == [
        {"type": "record", "data": {"file": "base64://abc"}},
        {"type": "video", "data": {"file": "file:///a.mp4"}},
        {"type": "file", "data": {"file": "file:///a.txt", "name": "资料.txt"}},
        {"type": "xml", "data": {"data": "<msg/>"}},
        {"type": "dice", "data": {}}, {"type": "rps", "data": {}},
    ]
    node = MessageSegment.node(456, "测试", [MessageSegment.at(789), MessageSegment.text(" hi")])
    assert node.data == {"user_id": "456", "nickname": "测试", "content": [
        {"type": "at", "data": {"qq": "789"}}, {"type": "text", "data": {"text": " hi"}}]}
    assert MessageSegment.forward("fid").data == {"id": "fid"}
