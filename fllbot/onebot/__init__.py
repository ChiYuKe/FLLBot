"""OneBot 11 协议适配层。"""

from __future__ import annotations

from .models import (
    Event,
    EventBase,
    MessageEvent,
    MetaEvent,
    NoticeEvent,
    RequestEvent,
    Sender,
    UnknownEvent,
    event_kind,
    parse_event,
)
from .segments import (
    MessageSegment,
    as_segments,
    contains_at,
    normalize_message,
    parse_cq,
    plain_text,
    readable_text,
    to_cq,
)

__all__ = [
    "Event",
    "EventBase",
    "MessageEvent",
    "MessageSegment",
    "MetaEvent",
    "NoticeEvent",
    "RequestEvent",
    "Sender",
    "UnknownEvent",
    "as_segments",
    "contains_at",
    "event_kind",
    "normalize_message",
    "parse_cq",
    "parse_event",
    "plain_text",
    "readable_text",
    "to_cq",
]
