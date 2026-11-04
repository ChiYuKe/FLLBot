"""Pure reply planning with immutable targets, ordering and protocol formatting."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..onebot.segments import MessageSegment


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
        forward = options.reply_mode == 'forward' or (options.reply_mode == 'auto' and len(text)>=options.forward_threshold)
        parts = tuple(ReplyPart(index+int(forward),chunk,
                                message_id if options.quote_reply and index==0 else 0,
                                user_id if options.at_sender and scene=='group' and index==0 else 0)
                      for index,chunk in enumerate(chunks))
        return ReplyPlan(turn_id,self_id,scene,scene_id,'forward' if forward else 'split',text,parts,
                         nickname,options.reply_interval_ms)

def split_text(text: str, size: int) -> list[str]:
    """按长度切分长回复，尽量在标点处断开。"""
    text = (text or "").strip()
    if not text:
        return []
    if size <= 0 or len(text) <= size:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > size:
        window = remaining[:size]
        cut = max(
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind("；"),
            window.rfind("，"),
            window.rfind(" "),
        )
        if cut < size // 2:
            cut = size
        else:
            cut += 1
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        chunks.append(remaining.strip())
    return chunks

