"""OneBot 11 消息段：构造、CQ 码解析、纯文本提取。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_CQ_PATTERN = re.compile(r"\[CQ:([A-Za-z0-9_.\-]+)((?:,[^\]]*)?)\]")

_CQ_UNESCAPE = (
    ("&amp;", "&"),
    ("&#91;", "["),
    ("&#93;", "]"),
    ("&#44;", ","),
)

_PLACEHOLDERS = {
    "image": "[图片]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "markdown": "[卡片]",
    "forward": "[合并转发]",
    "node": "[合并转发]",
    "mface": "[表情]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "poke": "[戳一戳]",
    "music": "[音乐]",
    "contact": "[名片]",
    "location": "[位置]",
    "share": "[链接]",
    "reply": "",
    "at": "",
}


class MessageSegment(BaseModel):
    """单个消息段，例如 ``{"type": "text", "data": {"text": "hi"}}``。"""

    model_config = ConfigDict(extra="allow")

    type: str
    data: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": dict(self.data)}

    def __str__(self) -> str:  # 便于日志和调试
        return _segment_to_cq(self)

    # -- 构造糖 ----------------------------------------------------------- #
    @classmethod
    def text(cls, text: str) -> MessageSegment:
        return cls(type="text", data={"text": text})

    @classmethod
    def at(cls, qq: int | str) -> MessageSegment:
        return cls(type="at", data={"qq": str(qq)})

    @classmethod
    def at_all(cls) -> MessageSegment:
        return cls(type="at", data={"qq": "all"})

    @classmethod
    def reply(cls, message_id: int | str) -> MessageSegment:
        return cls(type="reply", data={"id": str(message_id)})

    @classmethod
    def image(cls, file: str | None = None, *, url: str | None = None) -> MessageSegment:
        data: dict[str, Any] = {}
        if file:
            data["file"] = file
        if url:
            data["file"] = url
        return cls(type="image", data=data)

    @classmethod
    def face(cls, face_id: int | str) -> MessageSegment:
        return cls(type="face", data={"id": str(face_id)})

    @classmethod
    def poke(cls, qq: int | str) -> MessageSegment:
        return cls(type="poke", data={"qq": str(qq)})

    @classmethod
    def json_card(cls, payload: str) -> MessageSegment:
        return cls(type="json", data={"data": payload})

    # -- 便捷查询 --------------------------------------------------------- #
    @property
    def is_text(self) -> bool:
        return self.type == "text"

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def text_content(self) -> str:
        return str(self.data.get("text", "")) if self.type == "text" else ""


def _unescape(value: str) -> str:
    for escaped, plain in _CQ_UNESCAPE:
        value = value.replace(escaped, plain)
    return value


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;").replace(",", "&#44;")


def _segment_to_cq(segment: MessageSegment) -> str:
    if segment.type == "text":
        return _escape(segment.text_content)
    params = ",".join(f"{key}={_escape(str(value))}" for key, value in segment.data.items())
    return f"[CQ:{segment.type}{',' + params if params else ''}]"


def parse_cq(text: str) -> list[MessageSegment]:
    """把 CQ 码字符串解析成消息段列表。"""
    segments: list[MessageSegment] = []
    cursor = 0
    for match in _CQ_PATTERN.finditer(text):
        if match.start() > cursor:
            segments.append(MessageSegment.text(_unescape(text[cursor : match.start()])))
        cq_type = match.group(1)
        data: dict[str, Any] = {}
        raw_params = match.group(2)
        if raw_params:
            for pair in raw_params[1:].split(","):
                key, sep, value = pair.partition("=")
                if sep:
                    data[key.strip()] = _unescape(value)
        segments.append(MessageSegment(type=cq_type, data=data))
        cursor = match.end()
    if cursor < len(text):
        segments.append(MessageSegment.text(_unescape(text[cursor:])))
    return segments


def normalize_message(raw: Any) -> list[MessageSegment]:
    """把 NapCat 传来的 message 字段统一成消息段列表（兼容字符串 / 数组 / 单段）。"""
    if raw is None:
        return []
    if isinstance(raw, MessageSegment):
        return [raw]
    if isinstance(raw, str):
        return parse_cq(raw)
    if isinstance(raw, dict):
        if "type" in raw:
            return [MessageSegment.model_validate(raw)]
        return []
    if isinstance(raw, (list, tuple)):
        segments: list[MessageSegment] = []
        for item in raw:
            if isinstance(item, MessageSegment):
                segments.append(item)
            elif isinstance(item, dict) and "type" in item:
                segments.append(MessageSegment.model_validate(item))
            elif isinstance(item, str):
                segments.append(MessageSegment.text(item))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                # 兼容 [type, data] 这种老式写法
                seg_type, seg_data = item
                if isinstance(seg_data, dict):
                    segments.append(MessageSegment(type=str(seg_type), data=seg_data))
                else:
                    segments.append(MessageSegment(type=str(seg_type), data={"text": str(seg_data)}))
        return segments
    return []


def to_cq(segments: Sequence[MessageSegment]) -> str:
    """消息段列表转 CQ 码字符串（HTTP 上报或调试时可能用到）。"""
    return "".join(_segment_to_cq(segment) for segment in segments)


def plain_text(segments: Iterable[MessageSegment]) -> str:
    """只保留文本段并拼接（不含 @、图片等占位）。"""
    return "".join(segment.text_content for segment in segments if segment.is_text)


def readable_text(segments: Iterable[MessageSegment]) -> str:
    """带占位符的可读文本，适合日志、LLM 上下文与规则匹配。"""
    parts: list[str] = []
    for segment in segments:
        if segment.is_text:
            parts.append(segment.text_content)
        elif segment.type == "at":
            qq = segment.get("qq")
            parts.append(f"@{qq}")
        else:
            parts.append(_PLACEHOLDERS.get(segment.type, f"[{segment.type}]"))
    return "".join(parts)


def contains_at(segments: Iterable[MessageSegment], qq: int | str) -> bool:
    wanted = str(qq)
    return any(segment.type == "at" and str(segment.get("qq")) == wanted for segment in segments)


def strip_leading_at_self(segments: list[MessageSegment], self_id: int) -> list[MessageSegment]:
    """去掉开头连续的 @自己 段（以及其后的空白文本），用于 ``@机器人 /cmd`` 解析。"""
    result = list(segments)
    while result and result[0].type == "at" and str(result[0].get("qq")) == str(self_id):
        result.pop(0)
        if result and result[0].is_text:
            stripped = result[0].text_content.lstrip()
            if stripped:
                result[0] = MessageSegment.text(stripped)
            else:
                result.pop(0)
    return result


def as_segments(message: str | Sequence[MessageSegment] | None) -> list[MessageSegment]:
    """接受字符串或消息段，统一成列表。"""
    if message is None:
        return []
    if isinstance(message, str):
        return [MessageSegment.text(message)]
    return normalize_message(list(message))


__all__ = [
    "MessageSegment",
    "as_segments",
    "contains_at",
    "normalize_message",
    "parse_cq",
    "plain_text",
    "readable_text",
    "strip_leading_at_self",
    "to_cq",
]
