"""合并转发聊天记录：把 ``get_forward_msg`` 的结果展开成模型可读文本。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..onebot.segments import MessageSegment, image_url, normalize_message, readable_text

_logger = logging.getLogger("fllbot.llm.forward")

MAX_NODES = 50
MAX_DEPTH = 2
MAX_FETCHES = 6
MAX_CHARS = 4000
PLACEHOLDER = "[合并转发]"
_FORWARD_TYPES = frozenset({"forward", "node"})


@dataclass
class ForwardRead:
    limit: int
    deadline: float
    formatter: Any = None
    seen: set[str] = field(default_factory=set)
    fetches: int = 0
    nodes: int = 0
    errors: int = 0
    truncated: bool = False
    images: list[str] | None = None

    def clip(self, text: str) -> str:
        if len(text) > self.limit:
            self.truncated = True
        return _truncate(text,self.limit)


def has_forward(segments: Iterable[MessageSegment]) -> bool:
    """消息里是否包含合并转发段。"""
    return any(segment.type in _FORWARD_TYPES for segment in segments)


async def forward_transcript(
    connection: Any,
    segments: Iterable[MessageSegment],
    *,
    limit: int = MAX_CHARS,
    timeout: float = 5.0,
    metadata: dict | None = None,
    formatter: Any = None,
    images: list[str] | None = None,
) -> str:
    """展开消息中的合并转发记录；没有转发、读取失败或无内容时返回空串。"""
    blocks: list[str] = []
    state = ForwardRead(limit,time.monotonic()+max(0.0,timeout),formatter)
    state.images = images
    for segment in segments:
        if segment.type not in _FORWARD_TYPES:
            continue
        block = await _render(connection, segment, depth=0, state=state)
        if block:
            blocks.append(block)
        if sum(len(block) for block in blocks) >= limit or state.nodes >= MAX_NODES:
            state.truncated = True
            break
    text = state.clip("\n\n".join(blocks))
    if metadata is not None:
        metadata.update({'kind':'forward','origin':'current_attachment','reference_ids':sorted(state.seen),
                         'nodes':state.nodes,'fetches':state.fetches,'errors':state.errors,'truncated':state.truncated})
    return text


def _forward_id(segment: MessageSegment) -> str:
    for key in ("id", "message_id", "resid", "m_resid"):
        value = segment.data.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _nodes(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    """从各家 OneBot 实现的返回结构里取出节点列表。"""
    if depth > 8:
        return []
    if isinstance(payload, dict):
        for key in ("messages", "message", "data", "content"):
            if key in payload:
                nodes = _nodes(payload[key],depth+1)
                if nodes:
                    return nodes
        return []
    if isinstance(payload, (list, tuple)):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _unwrap_node(node: dict[str, Any]) -> dict[str, Any]:
    """兼容 ``{"type": "node", "data": {...}}`` 这种节点包装。"""
    if node.get("type") == "node" and isinstance(node.get("data"), dict):
        return node["data"]
    return node


def _node_name(node: dict[str, Any]) -> str:
    node = _unwrap_node(node)
    sender = node.get("sender")
    candidates: list[Any] = []
    if isinstance(sender, dict):
        candidates += [sender.get("card"), sender.get("nickname"), sender.get("user_id")]
    candidates += [node.get("card"), node.get("nickname"), node.get("user_id")]
    for value in candidates:
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _node_segments(node: dict[str, Any]) -> list[MessageSegment]:
    node = _unwrap_node(node)
    content = node.get("message")
    if content is None:
        content = node.get("content")
    if content is None:
        content = node.get("raw_message")
    return normalize_message(content)


async def _render(
    connection: Any,
    segment: MessageSegment,
    *,
    depth: int,
    state: ForwardRead,
) -> str:
    if depth > MAX_DEPTH:
        state.truncated = True
        return ""
    if segment.type == "node":
        return await _render_node(
            connection, segment.data, depth=depth + 1, state=state
        )
    forward_id = _forward_id(segment)
    if forward_id:
        if forward_id in state.seen or state.fetches >= MAX_FETCHES:
            state.truncated = True
            return ""
        state.seen.add(forward_id)
        state.fetches += 1
        try:
            remaining = state.deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            payload = await asyncio.wait_for(connection.get_forward_msg(forward_id),remaining)
        except Exception as exc:
            state.errors += 1
            _logger.warning("读取合并转发失败（%s）",type(exc).__name__)
            return ""
        nodes = _nodes(payload)
    else:
        # 少数实现直接把节点内容放在 forward 段里。
        nodes = _nodes(segment.data)
    if not nodes:
        return ""
    lines = [f"【合并转发·共{len(nodes)}条】"]
    for node in nodes[:MAX_NODES]:
        if state.nodes >= MAX_NODES:
            state.truncated = True
            break
        lines.append(
            await _render_node(connection, node, depth=depth + 1, state=state)
        )
        if sum(len(line) for line in lines) >= state.limit:
            state.truncated = True
            break
    if len(nodes) > MAX_NODES:
        lines.append(f"……其余 {len(nodes) - MAX_NODES} 条已省略")
    return state.clip("\n".join(lines))


async def _render_node(
    connection: Any,
    node: dict[str, Any],
    *,
    depth: int,
    state: ForwardRead,
) -> str:
    if state.nodes >= MAX_NODES:
        state.truncated = True
        return PLACEHOLDER
    state.nodes += 1
    name = _node_name(node)
    parts: list[str] = []
    for segment in _node_segments(node):
        if segment.type in _FORWARD_TYPES:
            nested = await _render(
                connection, segment, depth=depth, state=state
            )
            parts.append("\n" + nested + "\n" if nested else PLACEHOLDER)
        elif segment.type == "image" and state.images is not None and image_url(segment):
            url = image_url(segment)
            if url not in state.images and len(state.images) < 4:
                state.images.append(url)
            parts.append(f"[转发图片 {state.images.index(url) + 1}]" if url in state.images else "[图片·超出附图上限]")
        else:
            parts.append(readable_text([segment]))
        if sum(len(part) for part in parts) >= state.limit:
            state.truncated = True
            break
    body = state.clip("".join(parts).strip() or "[空消息]")
    if state.formatter is not None:
        original = _unwrap_node(node)
        sender = original.get('sender')
        if not isinstance(sender,dict):
            sender = original
        return state.clip(state.formatter(sender,original,body))
    if name:
        separator = "\n" if body.startswith("【合并转发") else ""
        return f"{name}：{separator}{body}"
    return body


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"

