"""合并转发聊天记录：把 ``get_forward_msg`` 的结果展开成模型可读文本。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from ..onebot.segments import MessageSegment, normalize_message, readable_text

_logger = logging.getLogger("fllbot.llm.forward")

MAX_NODES = 50
MAX_DEPTH = 2
MAX_FETCHES = 6
MAX_CHARS = 4000
PLACEHOLDER = "[合并转发]"
_FORWARD_TYPES = frozenset({"forward", "node"})


def has_forward(segments: Iterable[MessageSegment]) -> bool:
    """消息里是否包含合并转发段。"""
    return any(segment.type in _FORWARD_TYPES for segment in segments)


async def forward_transcript(
    connection: Any,
    segments: Iterable[MessageSegment],
    *,
    limit: int = MAX_CHARS,
) -> str:
    """展开消息中的合并转发记录；没有转发、读取失败或无内容时返回空串。"""
    blocks: list[str] = []
    seen: set[str] = set()
    fetches = [0]
    for segment in segments:
        if segment.type not in _FORWARD_TYPES:
            continue
        block = await _render(connection, segment, depth=0, seen=seen, fetches=fetches)
        if block:
            blocks.append(block)
    return _truncate("\n\n".join(blocks), limit)


def _forward_id(segment: MessageSegment) -> str:
    for key in ("id", "message_id", "resid", "m_resid"):
        value = segment.data.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _nodes(payload: Any) -> list[dict[str, Any]]:
    """从各家 OneBot 实现的返回结构里取出节点列表。"""
    if isinstance(payload, dict):
        for key in ("messages", "message", "data", "content"):
            if key in payload:
                nodes = _nodes(payload[key])
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
    seen: set[str],
    fetches: list[int],
) -> str:
    if depth > MAX_DEPTH:
        return ""
    if segment.type == "node":
        return await _render_node(
            connection, segment.data, depth=depth + 1, seen=seen, fetches=fetches
        )
    forward_id = _forward_id(segment)
    if forward_id:
        if forward_id in seen or fetches[0] >= MAX_FETCHES:
            return ""
        seen.add(forward_id)
        fetches[0] += 1
        try:
            payload = await connection.get_forward_msg(forward_id)
        except Exception:
            _logger.warning("读取合并转发失败：%s", forward_id, exc_info=True)
            return ""
        nodes = _nodes(payload)
    else:
        # 少数实现直接把节点内容放在 forward 段里。
        nodes = _nodes(segment.data)
    if not nodes:
        return ""
    lines = [f"【合并转发·共{len(nodes)}条】"]
    for node in nodes[:MAX_NODES]:
        lines.append(
            await _render_node(connection, node, depth=depth + 1, seen=seen, fetches=fetches)
        )
    if len(nodes) > MAX_NODES:
        lines.append(f"……其余 {len(nodes) - MAX_NODES} 条已省略")
    return "\n".join(lines)


async def _render_node(
    connection: Any,
    node: dict[str, Any],
    *,
    depth: int,
    seen: set[str],
    fetches: list[int],
) -> str:
    name = _node_name(node)
    parts: list[str] = []
    for segment in _node_segments(node):
        if segment.type in _FORWARD_TYPES:
            nested = await _render(
                connection, segment, depth=depth, seen=seen, fetches=fetches
            )
            parts.append("\n" + nested + "\n" if nested else PLACEHOLDER)
        else:
            parts.append(readable_text([segment]))
    body = "".join(parts).strip() or "[空消息]"
    if name:
        separator = "\n" if body.startswith("【合并转发") else ""
        return f"{name}：{separator}{body}"
    return body


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"

