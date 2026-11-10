"""Dedicated tree layout and obstacle routing regression coverage."""
import io

from PIL import Image

from fllbot.rendering.code_render import code_rows
from fllbot.rendering.diagram_render import parse_diagram, render_diagram
from fllbot.rendering.elk_render import collect_geometry, layout_graph, prepare_graph
from fllbot.rendering.text_image import load_font, render
from fllbot.rendering.tree_render import is_tree, tree_rows

TREE = "仓库/\n├── Core/  核心功能\n│   └── VeryLongModuleIdentifier/  " + "完整说明" * 15 + "\n└── README.md  文档"


def test_tree_keeps_guides_and_wraps_annotations_at_narrow_width():
    font = load_font("", 24)
    rows = list(tree_rows(TREE, font, 400))
    assert is_tree(TREE)
    assert len(rows) == 4  # root becomes the document title
    assert rows[2].height > rows[1].height
    for row in rows:
        assert row.width == 400
    pages = render(TREE, format="tree", width=400)
    with Image.open(io.BytesIO(pages[0])) as image:
        assert image.height <= 1800


def test_text_fences_detect_trees_without_treating_code_as_a_tree():
    font = load_font("", 24)
    automatic = list(code_rows(TREE, "text", font, 900))
    explicit = list(tree_rows(TREE, font, 900))
    assert [row.tobytes() for row in automatic] == [row.tobytes() for row in explicit]
    assert not is_tree("def f():\n    return 1")


def test_compound_edges_attach_to_their_actual_node_boundaries():
    source = "flowchart TD\nsubgraph 外层\nA[入口]\nB[服务]\nend\nsubgraph 存储\nC[(数据库)]\nend\nA --> B\nB -->|存储| C\nC -.-> A"
    graph, _ = prepare_graph(parse_diagram(source), load_font("", 24))
    nodes, edges = collect_geometry(layout_graph(graph))
    boxes = {item['id']: (x, y, x + item['width'], y + item['height']) for item, x, y in nodes}

    def on_border(point, bounds):
        x, y = point
        left, top, right, bottom = bounds
        return (abs(x - left) < .1 or abs(x - right) < .1) and top - .1 <= y <= bottom + .1 or (
            (abs(y - top) < .1 or abs(y - bottom) < .1) and left - .1 <= x <= right + .1)

    assert len(edges) == 3
    for edge, ox, oy in edges:
        section = edge['sections'][0]
        first, last = section['startPoint'], section['endPoint']
        assert on_border((first['x'] + ox, first['y'] + oy), boxes[edge['sources'][0]])
        assert on_border((last['x'] + ox, last['y'] + oy), boxes[edge['targets'][0]])


def test_wide_group_wraps_and_direct_edge_labels_render():
    source = "flowchart TD\nsubgraph 核心\n" + "\n".join(f"N{i}[模块 {i}]" for i in range(7))
    source += "\nend\nN0 -->|直接调用| N1\nN1 --> N6\nN6 -.->|回调| N0"
    with Image.open(io.BytesIO(render_diagram(source, title="分组折行"))) as image:
        assert image.width < 3200
        assert image.height < 4800


def test_nested_groups_can_render_without_blocking_ports():
    source = "flowchart TD\nsubgraph 外层\nsubgraph 内层\nA[入口]\nend\nB[服务]\nend\nA -->|调用| B"
    assert render_diagram(source).startswith(b"\x89PNG")
