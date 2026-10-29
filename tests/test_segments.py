"""消息段与 CQ 码测试。"""

from __future__ import annotations

from fllbot.onebot.segments import (
    MessageSegment,
    contains_at,
    normalize_message,
    parse_cq,
    plain_text,
    readable_text,
    strip_leading_at_self,
    to_cq,
)


def test_parse_simple_cq() -> None:
    segments = parse_cq("[CQ:at,qq=123456] 你好")
    assert [seg.type for seg in segments] == ["at", "text"]
    assert segments[0].data["qq"] == "123456"
    assert segments[1].data["text"] == " 你好"


def test_parse_cq_with_escaped_values() -> None:
    segments = parse_cq("[CQ:at,qq=1,name=a&#44;b]")
    assert segments[0].data["name"] == "a,b"


def test_parse_plain_text_only() -> None:
    segments = parse_cq("just text")
    assert len(segments) == 1
    assert segments[0].type == "text"
    assert plain_text(segments) == "just text"


def test_normalize_variants() -> None:
    assert normalize_message("hi")[0].text_content == "hi"
    assert normalize_message([{"type": "text", "data": {"text": "hi"}}])[0].text_content == "hi"
    assert normalize_message({"type": "text", "data": {"text": "hi"}})[0].type == "text"
    assert normalize_message(None) == []


def test_to_cq_roundtrip() -> None:
    original = [MessageSegment.at(42), MessageSegment.text("看这里")]
    cq = to_cq(original)
    assert cq == "[CQ:at,qq=42]看这里"
    assert plain_text(parse_cq(cq)) == "看这里"


def test_readable_text_placeholders() -> None:
    segments = [MessageSegment.at_all(), MessageSegment.image("a.png"), MessageSegment.text("hi")]
    text = readable_text(segments)
    assert "@all" in text
    assert "[图片]" in text
    assert text.endswith("hi")


def test_contains_at() -> None:
    segments = [MessageSegment.at(10001), MessageSegment.text("hi")]
    assert contains_at(segments, 10001)
    assert not contains_at(segments, 10002)


def test_strip_leading_at_self() -> None:
    segments = [MessageSegment.at(10001), MessageSegment.text("  /ping")]
    stripped = strip_leading_at_self(segments, 10001)
    assert plain_text(stripped) == "/ping"


def test_segment_cq_str() -> None:
    assert str(MessageSegment.text("a,b")) == "a&#44;b"
    assert str(MessageSegment.at(1)) == "[CQ:at,qq=1]"
