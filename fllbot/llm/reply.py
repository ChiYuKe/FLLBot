"""Pure reply planning with immutable targets, ordering and protocol formatting."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..onebot.segments import MessageSegment

# 句子/段落结束符：优先在这些位置断开，保证分段之间的语义完整。
_STRONG_END = "。！？!?…；;\n"
# 紧跟在结束符后的收尾符号，应与前一句留在同一段。
_CLOSERS = "”’」』】）》〉\"'"
# 分句符号：找不到句子边界时才在分句处断开。
_CLAUSE_END = "，、：:,"
# 行内 Markdown 结构：这些区间内部不能断开，否则 **、`code` 等会跨消息残缺。
_PROTECTED = (
    re.compile(r"`[^`\n]{1,200}`"),
    re.compile(r"\*\*[^\n]{1,400}?\*\*"),
    re.compile(r"__[^\n]{1,400}?__"),
    re.compile(r"~~[^\n]{1,400}?~~"),
    re.compile(r"!?\[[^\]\n]{0,200}\]\([^)\n]{0,500}\)"),
)


@dataclass(frozen=True)
class ReplyPart:
    index: int
    text: str
    quote_id: int = 0
    at_user_id: int = 0

    def segments(self) -> list[MessageSegment]:
        result = []
        if self.quote_id:
            result.append(MessageSegment.reply(self.quote_id))
        if self.at_user_id:
            result.extend([MessageSegment.at(self.at_user_id), MessageSegment.text(' ')])
        result.append(MessageSegment.text(self.text))
        return result


@dataclass(frozen=True)
class ReplyPlan:
    turn_id: str
    self_id: int
    scene: str
    scene_id: int
    mode: str
    candidate: str
    parts: tuple[ReplyPart, ...]
    nickname: str
    interval_ms: int

    def persisted_parts(self) -> list[str]:
        return ([self.candidate] if self.mode == 'forward' else []) + [part.text for part in self.parts]

    def formatting(self) -> dict:
        return {'format_version':1,'part_segments':{str(part.index):[segment.to_dict() for segment in part.segments()]
                                                   for part in self.parts},
                'nickname':self.nickname,'interval_ms':self.interval_ms}


class ReplyService:
    def build(self, text: str, *, turn_id: str, self_id: int, scene: str, scene_id: int,
              message_id: int, user_id: int, options: Any, default_chunk_size: int,
              nickname: str = 'FLLBot') -> ReplyPlan:
        chunks = split_text(text, options.reply_chunk_size or default_chunk_size)
        image = options.reply_mode == 'image' or (
            options.reply_mode == 'auto_image' and len(text) >= options.forward_threshold)
        forward = options.reply_mode == 'forward' or (
            options.reply_mode == 'auto' and len(text) >= options.forward_threshold)
        mode = 'image' if image else 'forward' if forward else 'split'
        parts = tuple(ReplyPart(index+int(forward),chunk,
                                message_id if options.quote_reply and index==0 else 0,
                                user_id if options.at_sender and scene=='group' and index==0 else 0)
                      for index,chunk in enumerate(chunks))
        return ReplyPlan(turn_id,self_id,scene,scene_id,mode,text,parts,
                         nickname,options.reply_interval_ms)


def split_text(text: str, size: int) -> list[str]:
    """按语义边界切分长回复。

    - 优先在段落、句子结束处断开，其次分句标点，最后按空格或长度硬切；
    - 不拆开行内 Markdown（加粗、行内代码、链接），超长代码块按行断开；
    - 过短的尾段并入前一段，避免最后一条只剩半句话。
    """
    text = (text or "").strip()
    if not text:
        return []
    if size <= 0 or len(text) <= size:
        return [text]

    spans = _protected_spans(text)
    chunks: list[str] = []
    starts: list[int] = []
    start = 0
    while len(text) - start > size:
        cut = _break_position(text, start, start + size, spans)
        if cut is None or cut <= start:
            cut = start + size
        chunk = text[start:cut].strip()
        if chunk:
            chunks.append(chunk)
            starts.append(start)
        start = cut
        while start < len(text) and text[start].isspace():
            start += 1
    tail = text[start:].strip()
    if tail:
        chunks.append(tail)
        starts.append(start)

    # 尾段过短说明切点落在一句话的末段，宁可让前一段略长也不要发一条碎片消息。
    if len(chunks) > 1 and len(chunks[-1]) < max(1, size // 8):
        merged = text[starts[-2]:].strip()
        chunks[-2:] = [merged]
    return chunks


def _protected_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for pattern in _PROTECTED for match in pattern.finditer(text)]


def _is_inside(spans: list[tuple[int, int]], position: int) -> bool:
    return any(start < position < end for start, end in spans)


def _break_position(text: str, start: int, limit: int, spans: list[tuple[int, int]]) -> int | None:
    """在 [start, limit] 内选择最靠后的自然断点，找不到时返回 None。"""
    window = limit - start
    middle = start + max(1, window // 2)
    tail_floor = start + max(1, window * 3 // 5)
    cut = _last_boundary(text, limit, tail_floor, _STRONG_END, spans)
    if cut is not None:
        return cut
    cut = _last_boundary(text, limit, middle, _CLAUSE_END, spans)
    if cut is None:
        cut = _last_space(text, limit, middle, spans)
    if cut is None:
        return None
    # 剩下太少会并入前一段；否则宁可退到句末，也不让下一段从半句话开始。
    if len(text) - cut < max(1, window // 8):
        return cut
    sentence = _last_boundary(text, limit, middle, _STRONG_END, spans)
    return sentence if sentence is not None else cut


def _last_boundary(
    text: str, limit: int, floor: int, markers: str, spans: list[tuple[int, int]]
) -> int | None:
    for index in range(min(limit, len(text)) - 1, floor - 1, -1):
        if text[index] not in markers:
            continue
        cut = index + 1
        if text[index] != "\n":
            while cut < len(text) and text[cut] in _CLOSERS:
                cut += 1
        if not _is_inside(spans, cut):
            return cut
    return None


def _last_space(text: str, limit: int, floor: int, spans: list[tuple[int, int]]) -> int | None:
    for index in range(min(limit, len(text)) - 1, floor - 1, -1):
        if text[index].isspace():
            cut = index + 1
            if not _is_inside(spans, cut):
                return cut
    return None
