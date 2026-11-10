"""Shared local Mermaid flowchart renderer for architecture diagrams.

Supports a bounded flowchart subset (TD/LR, node shapes, labelled edges, nested
subgraphs). ELK.js computes compound graph geometry through a local Node.js
process; Pillow draws PNGs. No browser or network is used at render time.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

MAX_SOURCE_CHARS = 6_000
MAX_NODES = 60
MAX_EDGES = 140
MAX_NODE_LABEL = 200

_HEADER = re.compile(r"^(?:flowchart|graph)\s+(TD|TB|BT|LR|RL)$", re.IGNORECASE)
_ARROW_TOKEN = re.compile(r"-\.+->|-\.+-|-{2,}>|-{2,}|={2,}>|={2,}")
_ARROW = re.compile(r"\s*(-\.+->|-\.+-|-{2,}>|-{2,}|={2,}>|={2,})\s*")
_IGNORED_PREFIXES = (
    "classdef",
    "class ",
    "style ",
    "linkstyle",
    "click ",
    "direction ",
    "acctitle",
    "accdescr",
    "%%",
)



@dataclass
class DiagramNode:
    id: str
    label: str
    shape: str = "rect"
    group: str = ""
    order: int = 0


@dataclass
class DiagramEdge:
    source: str
    target: str
    label: str = ""
    style: str = "solid"
    order: int = 0


@dataclass
class DiagramGroup:
    id: str
    title: str
    parent: str = ""
    order: int = 0


@dataclass
class Diagram:
    direction: str = "TD"
    nodes: dict[str, DiagramNode] = field(default_factory=dict)
    edges: list[DiagramEdge] = field(default_factory=list)
    groups: dict[str, DiagramGroup] = field(default_factory=dict)


def parse_diagram(source: str) -> Diagram:
    """Parse the supported Mermaid flowchart subset; raise ValueError with a safe message."""
    text = str(source or "").strip()
    if not text:
        raise ValueError("架构图内容不能为空")
    if len(text) > MAX_SOURCE_CHARS:
        raise ValueError(f"架构图源码最多 {MAX_SOURCE_CHARS} 个字符，请拆分")
    diagram = Diagram()
    stack: list[str] = []
    group_counter = 0
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip().rstrip(";").strip()
        if not line or line.startswith("```"):
            continue
        header = _HEADER.match(line)
        if header:
            direction = header.group(1).upper()
            diagram.direction = "LR" if direction in {"LR", "RL"} else "TD"
            continue
        lowered = line.lower()
        if any(lowered.startswith(prefix) for prefix in _IGNORED_PREFIXES):
            continue
        if lowered == "end":
            if stack:
                stack.pop()
            continue
        if lowered.startswith("subgraph"):
            group_counter += 1
            gid, title = _parse_subgraph(line, group_counter)
            diagram.groups[gid] = DiagramGroup(
                id=gid, title=title or "分组", parent=stack[-1] if stack else "", order=group_counter
            )
            stack.append(gid)
            continue
        line = re.sub(r":::[A-Za-z0-9_-]+", "", line)
        line = re.sub(
            r"--\s+([^|>\n]+?)\s+-->",
            lambda match: f"-->|{match.group(1).strip()}|",
            line,
        )
        _parse_statement(line, diagram, stack)
    if not diagram.nodes:
        raise ValueError("架构图中没有可绘制的节点，请使用 flowchart TD 和 A[名称] 定义节点")
    if len(diagram.nodes) > MAX_NODES:
        raise ValueError(f"节点最多 {MAX_NODES} 个，请合并模块或拆成多张图")
    unique: list[DiagramEdge] = []
    seen: set[tuple[str, str, str, str]] = set()
    for edge in diagram.edges:
        key = (edge.source, edge.target, edge.label, edge.style)
        if key not in seen:
            seen.add(key)
            unique.append(edge)
    diagram.edges = unique
    if len(diagram.edges) > MAX_EDGES:
        raise ValueError(f"连线最多 {MAX_EDGES} 条，请精简关系")
    return diagram


def _parse_subgraph(line: str, counter: int) -> tuple[str, str]:
    rest = line[len("subgraph"):].strip()
    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "\"'":
        return f"g{counter}", _clean_label(rest[1:-1])
    match = re.match(r"^(\S+)\s*\[(.*)\]$", rest)
    if match:
        return f"g{counter}", _clean_label(match.group(2))
    return f"g{counter}", _clean_label(rest)


def _clean_label(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    for marker in ("<br/>", "<br />", "<br>"):
        text = text.replace(marker, "\n")
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\\n", "\n")
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) > MAX_NODE_LABEL:
        raise ValueError(f"节点或连线标签最多 {MAX_NODE_LABEL} 个字符")
    return text


def _read_node(line: str, pos: int) -> tuple[str, str, str, int]:
    size = len(line)
    while pos < size and line[pos].isspace():
        pos += 1
    start = pos
    candidate = _ARROW_TOKEN.search(line, start)
    if candidate is not None:
        node_id = line[start : candidate.start()].rstrip()
        if node_id and all(char.isalnum() or char in "._-:" for char in node_id):
            if len(node_id) > 64:
                raise ValueError("节点标识最多 64 个字符")
            return node_id, "", "rect", candidate.start()
    while pos < size and (line[pos].isalnum() or line[pos] in "._-:"):
        pos += 1
    if pos == start:
        raise ValueError(f"无法解析的架构图语句：{line[:80]}")
    node_id = line[start:pos]
    if len(node_id) > 64:
        raise ValueError("节点标识最多 64 个字符")
    label, shape = "", "rect"
    save = pos
    while pos < size and line[pos].isspace():
        pos += 1
    if pos < size and line[pos] in "[({":
        opening = line[pos]
        if opening == "[":
            if line.startswith("[(", pos):
                shape, closer = "cylinder", ")]"
                pos += 2
            else:
                shape = "rect"
                closer = "]]" if line.startswith("[[", pos) else "]"
                pos += 2 if closer == "]]" else 1
        elif opening == "(":
            if line.startswith("([", pos):
                shape, closer = "pill", "])"
                pos += 2
            else:
                shape = "pill"
                closer = "))" if line.startswith("((", pos) else ")"
                pos += 2 if closer == "))" else 1
        elif line.startswith("{{", pos):
            shape, closer = "hexagon", "}}"
            pos += 2
        else:
            shape = "diamond"
            closer = "}"
            pos += 1
        end = line.find(closer, pos)
        if end < 0:
            raise ValueError(f"节点 {node_id} 的标签缺少收尾括号")
        label = _clean_label(line[pos:end])
        pos = end + len(closer)
    else:
        pos = save
    return node_id, label, shape, pos


def _edge_style(token: str) -> str:
    if token.endswith(">"):
        if token.startswith("-."):
            return "dashed"
        if token.startswith("="):
            return "thick"
        return "solid"
    if token.startswith("-."):
        return "dashed-line"
    if token.startswith("="):
        return "thick-line"
    return "line"


def _parse_statement(line: str, diagram: Diagram, stack: list[str]) -> None:
    pos = 0
    node_id, label, shape, pos = _read_node(line, pos)
    _ensure_node(diagram, node_id, label, shape, stack)
    while True:
        match = _ARROW.match(line, pos)
        if match is None:
            rest = line[pos:].strip()
            if rest.startswith("&"):
                raise ValueError("不支持 A & B 语法，请拆成两条连线")
            if rest:
                raise ValueError(f"无法解析的架构图语句：{line[:80]}")
            return
        pos = match.end()
        style = _edge_style(match.group(1))
        edge_label = ""
        if pos < len(line) and line[pos] == "|":
            end = line.find("|", pos + 1)
            if end < 0:
                raise ValueError("连线标签缺少右侧 |")
            edge_label = _clean_label(line[pos + 1 : end])
            pos = end + 1
        target, target_label, target_shape, pos = _read_node(line, pos)
        _ensure_node(diagram, target, target_label, target_shape, stack)
        diagram.edges.append(
            DiagramEdge(source=node_id, target=target, label=edge_label, style=style, order=len(diagram.edges))
        )
        node_id = target


def _ensure_node(diagram: Diagram, node_id: str, label: str, shape: str, stack: list[str]) -> None:
    existing = diagram.nodes.get(node_id)
    if existing is None:
        diagram.nodes[node_id] = DiagramNode(
            id=node_id,
            label=label or node_id,
            shape=shape,
            group=stack[-1] if stack else "",
            order=len(diagram.nodes),
        )
        return
    if label:
        existing.label = label
        existing.shape = shape
    if stack and not existing.group:
        existing.group = stack[-1]


def _wrap_label(text: str, font: Any, max_width: int, max_lines: int = 6) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for token in re.findall(r"[A-Za-z0-9_./:+%#@-]+|\s+|.", paragraph):
            if token.isspace():
                if current:
                    current += " "
                continue
            pieces = [token] if font.getlength(token) <= max_width else list(token)
            for piece in pieces:
                if current and font.getlength(current + piece) > max_width:
                    lines.append(current.rstrip())
                    current = piece
                else:
                    current += piece
        lines.append(current.rstrip())
    if len(lines) > max_lines:
        raise ValueError(f"节点标签最多 {max_lines} 行，请缩短：{text[:30]}…")
    return lines or [""]


def _dashed_line(draw: Any, first: tuple[float, float], second: tuple[float, float], fill: str, width: int) -> None:
    x1, y1 = first
    x2, y2 = second
    length = math.hypot(x2 - x1, y2 - y1)
    if length <= 0:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    dash, gap, offset = 9, 6, 0.0
    while offset < length:
        end = min(offset + dash, length)
        draw.line((x1 + ux * offset, y1 + uy * offset, x1 + ux * end, y1 + uy * end), fill=fill, width=width)
        offset = end + gap


def _arrowhead(draw: Any, tip: tuple[float, float], previous: tuple[float, float], fill: str, size: int) -> None:
    dx, dy = tip[0] - previous[0], tip[1] - previous[1]
    length = math.hypot(dx, dy)
    if length <= 0:
        return
    ux, uy = dx / length, dy / length
    base = (tip[0] - ux * size, tip[1] - uy * size)
    px, py = -uy, ux
    draw.polygon(
        [tip, (base[0] + px * size * 0.55, base[1] + py * size * 0.55),
         (base[0] - px * size * 0.55, base[1] - py * size * 0.55)],
        fill=fill,
    )


def render_diagram(
    source: str, *, title: str = "", font_path: str = "", font_size: int = 24
) -> bytes:
    """Render the bounded Mermaid subset with ELK's compound graph layout."""
    from .elk_render import render_elk
    from .text_image import load_font

    if not 16 <= font_size <= 48:
        raise ValueError("字号须为 16–48")
    diagram = parse_diagram(source)
    font = load_font(font_path, font_size)
    return render_elk(diagram, font, title.strip()[:120])
