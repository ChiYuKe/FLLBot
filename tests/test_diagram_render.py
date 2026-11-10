"""Architecture diagram renderer: Mermaid subset parsing, layout bounds and PNG output."""

import io

import pytest
from PIL import Image

from fllbot.rendering.diagram_render import MAX_NODES, parse_diagram, render_diagram

SAMPLE = """flowchart TD
  subgraph 客户端
    Web[Web 前端]
    App[桌面客户端]
  end
  subgraph 服务
    API[API 网关]
    DB[(PostgreSQL)]
  end
  Web --> API
  App -->|调用| API
  API --> DB
"""


def test_parser_reads_layers_nodes_and_edges():
    diagram = parse_diagram(SAMPLE)
    assert diagram.direction == "TD"
    assert [group.title for group in diagram.groups.values()] == ["客户端", "服务"]
    assert diagram.nodes["Web"].label == "Web 前端"
    assert diagram.nodes["DB"].shape == "cylinder"
    groups = {group.title: group.id for group in diagram.groups.values()}
    assert diagram.nodes["Web"].group == groups["客户端"]
    assert [(edge.source, edge.target, edge.label) for edge in diagram.edges] == [
        ("Web", "API", ""),
        ("App", "API", "调用"),
        ("API", "DB", ""),
    ]


def test_parser_accepts_compact_and_legacy_syntax():
    diagram = parse_diagram(
        "flowchart LR\nA-->B-.->|异步|C\nB -- 调用 --> D\nA:::hot"
    )
    assert diagram.direction == "LR"
    assert [edge.style for edge in diagram.edges] == ["solid", "dashed", "solid"]
    assert diagram.edges[1].label == "异步"
    assert diagram.edges[2].label == "调用"
    assert "A" in diagram.nodes


def test_parser_ignores_fences_comments_and_styles():
    source = """```mermaid
flowchart TD
%% 注释
classDef hot fill:#f00
A[入口]:::hot --> B[服务]
```"""
    diagram = parse_diagram(source)
    assert list(diagram.nodes) == ["A", "B"]


def test_parser_nested_subgraphs_and_shape_labels():
    diagram = parse_diagram(
        "flowchart TD\nsubgraph 外层\n  subgraph 内层\n    A[核心<br/>模块]\n  end\n"
        "  B(辅助)\n  D{{判断}}\n  E([开始])\nend"
    )
    outer = next(group for group in diagram.groups.values() if group.title == "外层")
    inner = next(group for group in diagram.groups.values() if group.title == "内层")
    assert inner.parent == outer.id
    assert diagram.nodes["A"].label == "核心\n模块"
    assert diagram.nodes["A"].group == inner.id
    assert diagram.nodes["B"].shape == "pill"
    assert diagram.nodes["D"].shape == "hexagon"
    assert diagram.nodes["D"].label == "判断"
    assert diagram.nodes["E"].shape == "pill"


def test_parser_rejects_invalid_content():
    cases = (
        ("", "不能为空"),
        ("flowchart TD", "没有可绘制"),
        ("flowchart TD\nA --> ", "无法解析"),
        ("flowchart TD\nA & B --> C", "&"),
        ("flowchart TD\nA[缺括号 --> B", "缺少"),
        ("flowchart TD\n" + "\n".join(f"N{index}[N{index}]" for index in range(MAX_NODES + 1)), "最多"),
    )
    for source, message in cases:
        with pytest.raises(ValueError, match=message):
            parse_diagram(source)


def test_render_outputs_png_with_expected_size_and_palette():
    data = render_diagram(SAMPLE, title="示例架构")
    assert data.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.width >= 640 and image.height >= 320
        colors = {color for _, color in image.convert("RGB").getcolors(maxcolors=1_000_000)}
    assert (247, 245, 239) in colors  # 米白画布
    assert (255, 254, 251) in colors  # 纸白节点


def test_render_lr_and_back_edge():
    data = render_diagram(
        "flowchart LR\nA[输入] --> B[服务]\nB --> C[存储]\nC -.->|回调| B", title="横向"
    )
    with Image.open(io.BytesIO(data)) as image:
        assert image.width > image.height


def test_render_rejects_oversize_and_bad_font():
    nodes = "\n".join(f"N{index}[N{index}]" for index in range(MAX_NODES))
    nodes += "\n" + "\n".join(f"N{index} --> N{index + 1}" for index in range(MAX_NODES - 1))
    with pytest.raises(ValueError, match="过大"):
        render_diagram("flowchart TD\n" + nodes)
    with pytest.raises(ValueError, match="中文字体"):
        render_diagram(SAMPLE, font_path="/nonexistent-font.ttf")
    with pytest.raises(ValueError, match="字号"):
        render_diagram(SAMPLE, font_size=10)
