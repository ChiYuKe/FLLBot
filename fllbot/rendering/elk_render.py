"""ELK compound layout with local Pillow typography and PNG output."""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import subprocess
from pathlib import Path

from .design_theme import ACCENT, CARD, INK, LINE, MUTED, PAPER


def layout_graph(graph):
    node = shutil.which("node")
    if not node:
        raise ValueError("架构图排版需要 Node.js 18 或更新版本，请安装后重启 FLLBot")
    try:
        result = subprocess.run(
            [node, "--max-old-space-size=256", str(Path(__file__).with_name("elk_layout.cjs"))],
            input=json.dumps(graph, ensure_ascii=False), capture_output=True,
            text=True, encoding="utf-8", timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("架构图布局超时，请减少节点或拆分关系") from exc
    except OSError as exc:
        raise ValueError("无法启动架构图布局引擎，请检查 Node.js 安装") from exc
    if result.returncode:
        raise ValueError("架构图布局失败，请简化分组或拆分关系")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ValueError("架构图布局引擎返回了无效结果") from exc


def prepare_graph(diagram, font):
    from .diagram_render import _wrap_label

    small = font.font_variant(size=round(font.size * .82))
    label_font = font.font_variant(size=round(font.size * .75))
    node_keys = {node.id: f"n{i}" for i, node in enumerate(diagram.nodes.values())}
    group_keys = {group.id: f"g{i}" for i, group in enumerate(diagram.groups.values())}
    direction = "DOWN" if diagram.direction == "TD" else "RIGHT"
    options = {
        "elk.algorithm": "layered", "elk.direction": direction,
        "elk.hierarchyHandling": "INCLUDE_CHILDREN", "elk.edgeRouting": "ORTHOGONAL",
        "elk.spacing.nodeNode": "38", "elk.spacing.edgeNode": "24", "elk.spacing.edgeEdge": "16",
        "elk.layered.spacing.nodeNodeBetweenLayers": "72",
        "elk.layered.spacing.edgeNodeBetweenLayers": "28",
        "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
        "elk.layered.crossingMinimization.semiInteractive": "true",
        "elk.padding": "[top=28,left=28,bottom=28,right=28]",
        "elk.randomSeed": "1",
    }
    graph = {"id": "root", "layoutOptions": options, "children": [], "edges": []}
    containers, typography = {"": graph}, {}
    for group in diagram.groups.values():
        titles = _wrap_label(group.title, small, 380, max_lines=6)
        title_height = len(titles) * round(small.size * 1.5)
        minimum = max(small.getlength(line) for line in titles) + 48
        entry = {"id": group_keys[group.id], "children": [],
                 "labels": [{"text": group.title, "width": minimum - 48, "height": title_height}],
                 "layoutOptions": {
            "elk.direction": "RIGHT",
            "elk.nodeLabels.placement": "[INSIDE, H_LEFT, V_TOP]",
            "elk.layered.spacing.nodeNodeBetweenLayers": "40",
            "elk.padding": f"[top={title_height + 32},left=24,bottom=24,right=24]",
            "elk.nodeSize.constraints": "MINIMUM_SIZE",
            "elk.nodeSize.minimum": f"({minimum},100)",
        }}
        typography[entry["id"]] = {"group": group, "titles": titles}
        containers[group.id] = entry
    for group in diagram.groups.values():
        containers[group.parent]["children"].append(containers[group.id])
    for node in diagram.nodes.values():
        parts = node.label.split("\n")
        primary = _wrap_label(parts[0], font, 340, max_lines=8)
        secondary = _wrap_label(" · ".join(parts[1:]), small, 340, max_lines=8) if len(parts) > 1 else []
        text_width = max([font.getlength(line) for line in primary] + [small.getlength(line) for line in secondary])
        width = max(164, math.ceil(text_width) + 44)
        height = 36 + len(primary) * round(font.size * 1.45) + len(secondary) * round(small.size * 1.5)
        if node.shape == "diamond":
            width = round(width * 1.7)
            height = round(height * 1.7)
        elif node.shape == "hexagon":
            width += 70
            height += 36
        if node.shape == "cylinder":
            height += 20
        entry = {"id": node_keys[node.id], "width": width, "height": height}
        containers[node.group]["children"].append(entry)
        typography[entry["id"]] = {"node": node, "primary": primary, "secondary": secondary}
    for i, edge in enumerate(diagram.edges):
        labels = _wrap_label(edge.label, label_font, 180, max_lines=6) if edge.label else []
        entry = {"id": f"e{i}", "sources": [node_keys[edge.source]], "targets": [node_keys[edge.target]]}
        if labels:
            entry["labels"] = [{"id": f"el{i}", "text": edge.label,
                                "width": math.ceil(max(label_font.getlength(line) for line in labels)) + 20,
                                "height": len(labels) * round(label_font.size * 1.5) + 12,
                                "layoutOptions": {"elk.edgeLabels.placement": "CENTER"}}]
        graph["edges"].append(entry)
        typography[entry["id"]] = {"edge": edge, "labels": labels}
    return graph, typography


def collect_geometry(layout):
    nodes, edges = [], []
    offsets = {layout["id"]: (0, 0)}

    def visit(container, ox, oy):
        for edge in container.get("edges", []):
            edges.append((edge, ox, oy))
        for child in container.get("children", []):
            x, y = ox + child.get("x", 0), oy + child.get("y", 0)
            nodes.append((child, x, y))
            offsets[child["id"]] = (x, y)
            visit(child, x, y)

    visit(layout, 0, 0)
    return nodes, [(edge, *offsets.get(edge.get("container"), (x, y))) for edge, x, y in edges]


def rounded_path(points, radius=10):
    result = [points[0]]
    for i, corner in enumerate(points[1:-1], 1):
        before, after = points[i - 1], points[i + 1]
        d1, d2 = math.dist(before, corner), math.dist(corner, after)
        if not d1 or not d2:
            continue
        r = min(radius, d1 / 2, d2 / 2)
        a = tuple(corner[j] + (before[j] - corner[j]) * r / d1 for j in (0, 1))
        b = tuple(corner[j] + (after[j] - corner[j]) * r / d2 for j in (0, 1))
        result.append(a)
        for step in range(1, 9):
            t = step / 8
            result.append(tuple((1 - t) ** 2 * a[j] + 2 * (1 - t) * t * corner[j] + t * t * b[j] for j in (0, 1)))
    result.append(points[-1])
    return result


def render_elk(diagram, font, title):
    from itertools import pairwise

    from PIL import Image, ImageDraw

    from .diagram_render import _arrowhead, _dashed_line, _wrap_label

    small = font.font_variant(size=round(font.size * .82))
    label_font = font.font_variant(size=round(font.size * .75))
    title_font = font.font_variant(size=round(font.size * 1.4))
    graph, typography = prepare_graph(diagram, font)
    layout = layout_graph(graph)
    nodes, edges = collect_geometry(layout)
    padding = 48
    title_lines = _wrap_label(title, title_font, 1600, max_lines=6) if title else []
    header = 28 + len(title_lines) * round(title_font.size * 1.5) if title_lines else 0
    width = math.ceil(max(layout["width"], max((title_font.getlength(t) for t in title_lines), default=0))) + padding * 2
    height = math.ceil(layout["height"]) + padding * 2 + header
    width, height = max(640, width), max(320, height)
    if width > 3200 or height > 4800:
        raise ValueError(f"架构图过大（{width}×{height} 像素），请减少节点或拆成多张")
    # Supersampling keeps thin connectors, curves and typography consistent.
    image = Image.new("RGB", (width * 2, height * 2), PAPER)
    draw = ImageDraw.Draw(image)
    ox, oy = padding, padding + header

    def box(x, y, w, h):
        return tuple(round(v * 2) for v in (x + ox, y + oy, x + ox + w, y + oy + h))

    def text(x, y, value, selected, color, **kwargs):
        draw.text((round((x + ox) * 2), round((y + oy) * 2)), value,
                  font=selected.font_variant(size=selected.size * 2), fill=color, **kwargs)

    for item, x, y in nodes:
        info = typography[item["id"]]
        if "group" not in info:
            continue
        bounds = box(x, y, item["width"], item["height"])
        draw.rounded_rectangle(bounds, radius=20, fill="#eeeee7", outline=LINE, width=2)
        group_label = item.get("labels", [{}])[0]
        for i, line in enumerate(info["titles"]):
            text(x + group_label.get("x", 20), y + group_label.get("y", 15) + i * round(small.size * 1.5),
                 line, small, MUTED)
    edge_labels = []
    for item, ex, ey in edges:
        info = typography[item["id"]]
        edge = info["edge"]
        color = ACCENT if edge.style.startswith("thick") else "#85978a"
        for section in item.get("sections", []):
            raw = [section["startPoint"], *section.get("bendPoints", []), section["endPoint"]]
            points = [(round((p["x"] + ex + ox) * 2), round((p["y"] + ey + oy) * 2)) for p in raw]
            smooth = rounded_path(points, 18)
            if edge.style.startswith("dashed"):
                for a, b in pairwise(smooth):
                    _dashed_line(draw, a, b, color, 3)
            else:
                draw.line(smooth, fill=color, width=5 if edge.style.startswith("thick") else 3, joint="curve")
            if not edge.style.endswith("line"):
                _arrowhead(draw, points[-1], points[-2], color, 13)
        for label in item.get("labels", []):
            edge_labels.append((label, info["labels"], ex, ey))
    for item, x, y in nodes:
        info = typography[item["id"]]
        if "node" not in info:
            continue
        node = info["node"]
        width_n, height_n = item["width"], item["height"]
        bounds = box(x, y, width_n, height_n)
        left, top, right, bottom = bounds
        if node.shape == "diamond":
            draw.polygon([((left + right) / 2, top), (right, (top + bottom) / 2),
                          ((left + right) / 2, bottom), (left, (top + bottom) / 2)], fill=CARD, outline="#b9c8bd", width=2)
        elif node.shape == "hexagon":
            inset = 36
            draw.polygon([(left + inset, top), (right - inset, top), (right, (top + bottom) / 2),
                          (right - inset, bottom), (left + inset, bottom), (left, (top + bottom) / 2)],
                         fill=CARD, outline="#b9c8bd", width=2)
        elif node.shape == "cylinder":
            draw.rounded_rectangle(bounds, radius=20, fill=CARD, outline="#b9c8bd", width=2)
            draw.arc((left, top, right, top + 36), 0, 180, fill="#b9c8bd", width=2)
        else:
            draw.rounded_rectangle(bounds, radius=(bottom - top) // 2 if node.shape == "pill" else 14,
                                   fill=CARD, outline="#c7cec3", width=2)
        primary, secondary = info["primary"], info["secondary"]
        total = len(primary) * round(font.size * 1.45) + len(secondary) * round(small.size * 1.5)
        ty = y + (height_n - total) / 2
        for line in primary:
            text(x + width_n / 2, ty, line, font, INK, anchor="ma")
            ty += round(font.size * 1.45)
        for line in secondary:
            text(x + width_n / 2, ty, line, small, MUTED, anchor="ma")
            ty += round(small.size * 1.5)
    for label, lines, ex, ey in edge_labels:
        bounds = box(label["x"] + ex, label["y"] + ey, label["width"], label["height"])
        draw.rounded_rectangle(bounds, radius=8, fill=PAPER)
        for i, line in enumerate(lines):
            text(label["x"] + ex + 10, label["y"] + ey + 4 + i * round(label_font.size * 1.5), line, label_font, MUTED)
    for i, line in enumerate(title_lines):
        text(0, -header + i * round(title_font.size * 1.5), line, title_font, INK)
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
