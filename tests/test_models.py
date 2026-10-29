"""OneBot 事件模型测试。"""

from __future__ import annotations

import pytest

from fllbot.onebot.models import (
    MessageEvent,
    MetaEvent,
    NoticeEvent,
    RequestEvent,
    UnknownEvent,
    event_kind,
    parse_event,
)
from helpers import group_message, heartbeat, lifecycle, private_message


def test_group_message() -> None:
    event = parse_event(group_message("/ping", at=10001))
    assert isinstance(event, MessageEvent)
    assert event.is_group
    assert event.group_id == 20002
    assert event.user_id == 30003
    assert event.at_me is True
    assert event.to_me is True
    assert event.text == "/ping"
    assert event.scene == "group"
    assert event.scene_id == 20002
    assert event.role == "member"
    assert event_kind(event) == "message"


def test_group_message_without_at() -> None:
    event = parse_event(group_message("hello", at=None))
    assert isinstance(event, MessageEvent)
    assert event.at_me is False
    assert event.to_me is False


def test_private_message_is_always_to_me() -> None:
    event = parse_event(private_message("hi"))
    assert isinstance(event, MessageEvent)
    assert event.is_private
    assert event.to_me is True
    assert event.scene == "private"
    assert event.scene_id == 30003


def test_message_as_cq_string() -> None:
    payload = group_message("x")
    payload["message"] = "[CQ:at,qq=10001]你好"
    event = parse_event(payload)
    assert isinstance(event, MessageEvent)
    assert event.at_me is True
    assert event.text == "你好"


def test_heartbeat_and_lifecycle() -> None:
    beat = parse_event(heartbeat())
    assert isinstance(beat, MetaEvent)
    assert beat.meta_event_type == "heartbeat"
    assert event_kind(beat) == "meta_event"

    life = parse_event(lifecycle())
    assert isinstance(life, MetaEvent)
    assert life.sub_type == "connect"


def test_notice_event() -> None:
    event = parse_event(
        {
            "time": 1,
            "self_id": 10001,
            "post_type": "notice",
            "notice_type": "group_ban",
            "sub_type": "ban",
            "group_id": 20002,
            "user_id": 30003,
            "operator_id": 30004,
            "duration": 60,
        }
    )
    assert isinstance(event, NoticeEvent)
    assert event.notice_type == "group_ban"
    assert event.duration == 60
    assert event_kind(event) == "notice"


def test_request_event() -> None:
    event = parse_event(
        {
            "time": 1,
            "self_id": 10001,
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "group_id": 20002,
            "user_id": 30003,
            "comment": "让我进群",
            "flag": "abc",
        }
    )
    assert isinstance(event, RequestEvent)
    assert event.flag == "abc"
    assert event_kind(event) == "request"


def test_unknown_post_type() -> None:
    event = parse_event({"post_type": "message_sent", "self_id": 1})
    assert isinstance(event, UnknownEvent)
    assert event_kind(event) is None


def test_group_id_zero_becomes_none() -> None:
    payload = group_message("hi")
    payload["group_id"] = 0
    event = parse_event(payload)
    assert isinstance(event, MessageEvent)
    assert event.group_id is None


@pytest.mark.parametrize("text", ["", "   ", "/"])
def test_text_extraction(text: str) -> None:
    event = parse_event(group_message(text))
    assert isinstance(event, MessageEvent)
    assert event.text == text.strip()
