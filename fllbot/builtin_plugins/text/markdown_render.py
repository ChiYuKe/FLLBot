"""Plugin: text | Role: local Markdown layout | Metadata: plugin.toml.

Parse Markdown tokens, never HTML or external resources. Bound all layout before
allocating page images, and split tall table rows/code blocks across pages.
"""

from __future__ import annotations

import io
from dataclasses import dataclass


@dataclass(frozen=True)
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    link: bool = False
    strike: bool = False


def inline_runs(tokens) -> list[Run]:
    style = {"bold": 0, "italic": 0, "link": 0, "strike": 0}
    names = {"strong": "bold", "em": "italic", "link": "link", "s": "strike"}
    runs = []
    for token in tokens or []:
        name, _, action = token.type.rpartition("_")
        if name in names and action in {"open", "close"}:
            key = names[name]
            style[key] += 1 if action == "open" else -1
        elif token.type in {"text", "code_inline", "softbreak", "hardbreak", "image"}:
            value = "\n" if token.type in {"softbreak", "hardbreak"} else token.content
            if token.type == "image":
                value = f"[图片：{value or '未提供说明'}]"
            runs.append(
                Run(value, code=token.type == "code_inline", **{key: value > 0 for key, value in style.items()})
            )
    return runs


def wrap(runs: list[Run], font, width: int) -> list[list[tuple[str, Run, float]]]:
    lines: list[list[tuple[str, Run, float]]] = [[]]
    used = 0.0
    for run in runs:
        for char in run.text.expandtabs(4):
            if char == "\r":
                continue
            size = font.getlength(char) + (1 if run.bold else 0)
            if char == "\n" or used + size > width and lines[-1]:
                lines.append([])
                used = 0
            if char != "\n":
                lines[-1].append((char, run, size))
                used += size
    return lines


def render_markdown(text: str, font, width: int) -> list[bytes]:
    from markdown_it import MarkdownIt
    from PIL import Image, ImageDraw

    parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    tokens = parser.parse(text)
    padding, page_height, card_margin = 58, 1800, 24
    # Each row: height, cells[(x, width, glyphs)], font, background, quote depth, border, heading.
    rows = []
    total_height = 0

    def add(cells, row_font, *, background=None, quote=0, border=False, heading=False, height=None):
        nonlocal total_height
        height = height or int(row_font.size * 1.65) + 8
        # Conservative page budget also accounts for space left at page breaks.
        total_height += height
        if total_height > (page_height - padding * 2 - 180) * 20:
            raise ValueError("排版超过 20 页，请缩短文本或增大图片宽度")
        rows.append((height, cells, row_font, background, quote, border, heading))

    quote_depth = 0
    lists = []
    prefix = ""
    heading = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == "blockquote_open":
            quote_depth += 1
        elif kind == "blockquote_close":
            quote_depth -= 1
        elif kind in {"bullet_list_open", "ordered_list_open"}:
            lists.append([kind == "ordered_list_open", int(token.attrGet("start") or 1)])
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
        elif kind == "list_item_open":
            ordered, number = lists[-1]
            prefix = f"{number}. " if ordered else "• "
            lists[-1][1] += 1
        elif kind == "heading_open":
            heading = int(token.tag[1:])
        elif kind == "heading_close":
            heading = 0
        elif kind == "hr":
            add([], font, border=True, height=24)
        elif kind == "table_open":
            table_rows, cells, header = [], [], False
            index += 1
            while tokens[index].type != "table_close":
                item = tokens[index]
                if item.type == "tr_open":
                    cells = []
                elif item.type == "th_open":
                    header = True
                elif item.type == "inline":
                    cells.append(inline_runs(item.children))
                elif item.type == "tr_close":
                    table_rows.append((cells, header))
                    header = False
                index += 1
            columns = max((len(cells) for cells, _ in table_rows), default=1)
            if columns > 12:
                raise ValueError("表格最多支持 12 列，请拆分表格")
            cell_width = (width - padding * 2) // columns
            if cell_width < font.size + 16:
                raise ValueError("表格列过多，请增大图片宽度或拆分表格")
            for cells, header in table_rows:
                wrapped = [wrap(cell, font, cell_width - 16) for cell in cells]
                for line in range(max(map(len, wrapped), default=1)):
                    add(
                        [
                            (padding + col * cell_width, cell_width, cell[line] if line < len(cell) else [])
                            for col, cell in enumerate(wrapped)
                        ],
                        font,
                        background="#e9eef5" if header else "#ffffff",
                        border=True,
                    )
        elif kind in {"inline", "fence", "code_block"}:
            is_code = kind != "inline"
            row_font = font.font_variant(size=round(font.size * (1.35 - heading * 0.06))) if heading else font
            indent = min(quote_depth * 16 + max(0, len(lists) - 1) * 20, (width - padding * 2) // 3)
            x = padding + indent
            runs = [Run(token.content.rstrip("\n"), code=True)] if is_code else inline_runs(token.children)
            if heading:
                runs = [Run(run.text, True, run.italic, run.code, run.link, run.strike) for run in runs]
            if prefix:
                runs.insert(0, Run(prefix))
                prefix = ""
            for line in wrap(runs, row_font, width - padding - x - 16):
                add(
                    [(x, width - padding - x, line)],
                    row_font,
                    background="#f3f5f7" if is_code else "#f5f7fa" if quote_depth else None,
                    quote=quote_depth,
                    heading=bool(heading),
                )
            if not lists:
                add([], font, height=18 if heading else 14)
        index += 1

    if not rows:
        add([], font)
    pages, page_rows, used = [], [], padding * 2
    for row in rows:
        if used + row[0] > page_height and page_rows:
            pages.append(page_rows)
            page_rows, used = [], padding * 2
        page_rows.append(row)
        used += row[0]
    if page_rows:
        pages.append(page_rows)
    if len(pages) > 20:
        raise ValueError("排版超过 20 页，请缩短文本或增大图片宽度")
    output = []
    for page in pages:
        image = Image.new("RGB", (width, padding * 2 + sum(row[0] for row in page)), "#eef1f6")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (card_margin, card_margin, width - card_margin, image.height - card_margin), radius=22, fill="#ffffff"
        )
        y = padding
        for height, cells, row_font, background, quote_depth, border, is_heading in page:
            if background:
                draw.rounded_rectangle((padding - 10, y, width - padding + 10, y + height), radius=8, fill=background)
            if is_heading:
                draw.rounded_rectangle((padding - 10, y + 3, padding - 6, y + height - 3), radius=2, fill="#4f7cac")
            if quote_depth:
                draw.rounded_rectangle((padding, y + 4, padding + 4, y + height - 4), radius=2, fill="#a8b5c5")
            if border and not cells:
                draw.line((padding, y + height // 2, width - padding, y + height // 2), fill="#ccd3dd")
            for x, cell_width, glyphs in cells:
                if border:
                    draw.rectangle((x, y, x + cell_width, y + height), outline="#ccd3dd")
                cursor = x + 8
                for char, run, advance in glyphs:
                    color = "#2563ad" if run.link else "#202124"
                    if run.code:
                        draw.rectangle((cursor, y + 2, cursor + advance, y + height - 2), fill="#e8ecf1")
                    if run.italic:
                        glyph = Image.new("RGBA", (int(advance) + height, height))
                        ImageDraw.Draw(glyph).text(
                            (height // 3, 3),
                            char,
                            font=row_font,
                            fill=color,
                            stroke_width=1 if run.bold else 0,
                            stroke_fill=color,
                        )
                        glyph = glyph.transform(
                            glyph.size,
                            Image.Transform.AFFINE,
                            (1, 0.2, -height * 0.15, 0, 1, 0),
                            resample=Image.Resampling.BICUBIC,
                        )
                        image.paste(glyph, (int(cursor) - height // 3, y), glyph)
                    else:
                        draw.text(
                            (cursor, y + 3),
                            char,
                            font=row_font,
                            fill=color,
                            stroke_width=1 if run.bold else 0,
                            stroke_fill=color,
                        )
                    if run.strike:
                        draw.line((cursor, y + height * 0.55, cursor + advance, y + height * 0.55), fill=color)
                    cursor += advance
            y += height
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        output.append(buffer.getvalue())
    return output
