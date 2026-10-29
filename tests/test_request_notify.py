"""好友/入群请求提醒与审批插件测试。"""

from __future__ import annotations

import time

from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message


def friend_request(flag: str = "flag-1", user_id: int = 30003) -> dict:
    return {
        "time": int(time.time()),
        "self_id": 10001,
        "post_type": "request",
        "request_type": "friend",
        "sub_type": "add",
        "user_id": user_id,
        "comment": "你好，我是测试",
        "flag": flag,
    }


def group_request(flag: str = "flag-2", user_id: int = 30003, group_id: int = 20002) -> dict:
    return {
        "time": int(time.time()),
        "self_id": 10001,
        "post_type": "request",
        "request_type": "group",
        "sub_type": "add",
        "group_id": group_id,
        "user_id": user_id,
        "comment": "求进群",
        "flag": flag,
    }


async def test_request_notifies_superuser(bot) -> None:
    conn = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(friend_request()), conn)  # type: ignore[arg-type]

    assert ("private", 99999) == (conn.sent[0][0], conn.sent[0][1])
    assert "flag-1" in conn.texts()[0]
    assert "好友申请" in conn.texts()[0]

    record = await bot.storage.kv_get("pending_requests", "flag-1")
    assert record["user_id"] == 30003
    assert record["request_type"] == "friend"


async def test_list_and_approve(bot) -> None:
    await bot.dispatcher.dispatch(parse_event(group_request()), FakeConnection())  # type: ignore[arg-type]

    listing = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message("/requests", user_id=99999)), listing)  # type: ignore[arg-type]
    assert "flag-2" in listing.last_text()

    approving = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message("/approve flag-2", user_id=99999)), approving)  # type: ignore[arg-type]
    calls = [call for call in approving.calls if call[0] == "set_group_add_request"]
    assert calls, "应当调用了 set_group_add_request"
    _, params = calls[0]
    assert params["approve"] is True
    assert params["sub_type"] == "add"
    assert await bot.storage.kv_get("pending_requests", "flag-2") is None


async def test_reject_with_reason(bot) -> None:
    await bot.dispatcher.dispatch(parse_event(friend_request(flag="flag-3")), FakeConnection())  # type: ignore[arg-type]

    conn = FakeConnection()
    await bot.dispatcher.dispatch(
        parse_event(group_message("/reject flag-3 不认识", user_id=99999)),  # type: ignore[arg-type]
        conn,
    )
    calls = [call for call in conn.calls if call[0] == "set_friend_add_request"]
    assert calls, "应当调用了 set_friend_add_request"
    _, params = calls[0]
    assert params["approve"] is False
    assert await bot.storage.kv_get("pending_requests", "flag-3") is None


async def test_approve_requires_superuser(bot) -> None:
    await bot.dispatcher.dispatch(parse_event(friend_request(flag="flag-4")), FakeConnection())  # type: ignore[arg-type]

    conn = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message("/approve flag-4", user_id=30003)), conn)  # type: ignore[arg-type]
    assert conn.actions() == []
    assert conn.texts() == []
    assert await bot.storage.kv_get("pending_requests", "flag-4") is not None
