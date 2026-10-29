"""OneBot 11 事件模型（NapCat 上报格式）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .segments import MessageSegment, contains_at, normalize_message, plain_text, readable_text


class Sender(BaseModel):
    """消息发送者信息。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    user_id: int | None = None
    nickname: str | None = None
    card: str | None = None
    role: str | None = None
    sex: str | None = None
    age: int | None = None
    area: str | None = None
    level: str | None = None
    title: str | None = None


class EventBase(BaseModel):
    """所有事件的公共字段。额外字段通过 ``extra="allow"`` 保留。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    time: int = 0
    self_id: int = 0
    post_type: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        value = getattr(self, key, None)
        if value is not None:
            return value
        extra = self.model_extra or {}
        return extra.get(key, default)

    def to_summary(self) -> dict[str, Any]:
        """给管理端/SSE 用的精简描述。"""
        return {
            "post_type": self.post_type,
            "self_id": self.self_id,
            "time": self.time,
        }


class MessageEvent(EventBase):
    """消息事件（群聊 / 私聊）。"""

    post_type: Literal["message"] = "message"
    message_type: Literal["private", "group", "guild"] = "private"
    sub_type: str = "normal"
    message_id: int = 0
    message_seq: int | None = None
    user_id: int = 0
    group_id: int | None = None
    target_id: int | None = None
    message: list[MessageSegment] = Field(default_factory=list)
    raw_message: str = ""
    font: int = 0
    sender: Sender = Field(default_factory=Sender)

    @field_validator("message", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> list[MessageSegment]:
        return normalize_message(value)

    @field_validator("group_id", mode="before")
    @classmethod
    def _zero_to_none(cls, value: Any) -> Any:
        return None if value in (0, "0", "") else value

    # -- 便捷属性 --------------------------------------------------------- #
    @property
    def is_group(self) -> bool:
        return self.message_type == "group"

    @property
    def is_private(self) -> bool:
        return self.message_type == "private"

    @property
    def scene(self) -> str:
        return "group" if self.is_group else "private"

    @property
    def scene_id(self) -> int:
        return int(self.group_id or 0) if self.is_group else int(self.user_id)

    @property
    def text(self) -> str:
        return plain_text(self.message).strip()

    @property
    def readable(self) -> str:
        return readable_text(self.message)

    @property
    def at_me(self) -> bool:
        return contains_at(self.message, self.self_id)

    @property
    def to_me(self) -> bool:
        """私聊默认算“对我说”，群聊只有 @ 才算。"""
        return self.is_private or self.at_me

    @property
    def nickname(self) -> str:
        return self.sender.card or self.sender.nickname or str(self.user_id)

    @property
    def role(self) -> str:
        return self.sender.role or "member"

    def to_summary(self) -> dict[str, Any]:
        data = super().to_summary()
        data.update(
            {
                "kind": "message",
                "message_type": self.message_type,
                "group_id": self.group_id,
                "user_id": self.user_id,
                "message_id": self.message_id,
                "text": self.readable[:500],
            }
        )
        return data


class NoticeEvent(EventBase):
    """通知事件：入群/退群、禁言、撤回、戳一戳等。"""

    post_type: Literal["notice"] = "notice"
    notice_type: str = ""
    sub_type: str = ""
    group_id: int | None = None
    user_id: int | None = None
    operator_id: int | None = None
    target_id: int | None = None
    message_id: int | None = None
    duration: int | None = None
    file: dict[str, Any] | None = None

    @field_validator("group_id", mode="before")
    @classmethod
    def _zero_to_none(cls, value: Any) -> Any:
        return None if value in (0, "0", "") else value

    def to_summary(self) -> dict[str, Any]:
        data = super().to_summary()
        data.update(
            {
                "kind": "notice",
                "notice_type": self.notice_type,
                "sub_type": self.sub_type,
                "group_id": self.group_id,
                "user_id": self.user_id,
                "operator_id": self.operator_id,
            }
        )
        return data


class RequestEvent(EventBase):
    """请求事件：加好友、加群/邀请。"""

    post_type: Literal["request"] = "request"
    request_type: str = ""
    sub_type: str = ""
    group_id: int | None = None
    user_id: int | None = None
    comment: str = ""
    flag: str = ""

    def to_summary(self) -> dict[str, Any]:
        data = super().to_summary()
        data.update(
            {
                "kind": "request",
                "request_type": self.request_type,
                "sub_type": self.sub_type,
                "group_id": self.group_id,
                "user_id": self.user_id,
                "comment": self.comment,
            }
        )
        return data


class MetaEvent(EventBase):
    """元事件：生命周期与心跳。"""

    post_type: Literal["meta_event"] = "meta_event"
    meta_event_type: str = ""
    sub_type: str = ""
    status: dict[str, Any] | None = None
    interval: int | None = None

    def to_summary(self) -> dict[str, Any]:
        data = super().to_summary()
        data.update({"kind": "meta_event", "meta_event_type": self.meta_event_type, "sub_type": self.sub_type})
        return data


class UnknownEvent(EventBase):
    """未识别的事件（例如 ``message_sent``），保留原始字段。"""

    post_type: str = ""
    kind: str = "unknown"

    def to_summary(self) -> dict[str, Any]:
        data = super().to_summary()
        data["kind"] = "unknown"
        return data


Event = MessageEvent | NoticeEvent | RequestEvent | MetaEvent | UnknownEvent

_EVENT_CLASSES: dict[str, type[EventBase]] = {
    "message": MessageEvent,
    "notice": NoticeEvent,
    "request": RequestEvent,
    "meta_event": MetaEvent,
}


def parse_event(payload: dict[str, Any]) -> Event:
    """把 NapCat 上报的 JSON 变成强类型事件。"""
    post_type = str(payload.get("post_type") or "")
    model = _EVENT_CLASSES.get(post_type, UnknownEvent)
    try:
        return model.model_validate(payload)
    except ValidationError:
        return UnknownEvent.model_validate(payload)


def event_kind(event: Event) -> str | None:
    """事件类型 → 分发用的种类名；不足关注的事件返回 ``None``。"""
    if isinstance(event, MessageEvent):
        return "message"
    if isinstance(event, NoticeEvent):
        return "notice"
    if isinstance(event, RequestEvent):
        return "request"
    if isinstance(event, MetaEvent):
        return "meta_event"
    return None


__all__ = [
    "Event",
    "EventBase",
    "MessageEvent",
    "MetaEvent",
    "NoticeEvent",
    "RequestEvent",
    "Sender",
    "UnknownEvent",
    "event_kind",
    "parse_event",
]
