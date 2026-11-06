"""Shared local markdown render implementation."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from itertools import groupby


@dataclass(frozen=True)
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    link: bool = False
    strike: bool = False
    block_code: bool = False


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
            value = "\n" if token.type == "hardbreak" else " " if token.type == "softbreak" else token.content
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
        # Keep Latin identifiers/words together whenever a full word fits.
        for word in re.findall(r"[A-Za-z0-9_./:-]+|[^A-Za-z0-9_./:-]", run.text.expandtabs(4)):
            word_size = font.getlength(word) + (len(word) if run.bold else 0)
            if len(word) > 1 and used + word_size > width and word_size <= width and lines[-1]:
                lines.append([])
                used = 0
            for char in word:
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
    from PIL import Image, ImageDraw, ImageFont

    parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    tokens = parser.parse(text)
    padding, page_height = 56, 1800
    # Each row: height, cells[(x, width, glyphs)], font, background, quote depth, border, heading.
    rows = []
    total_height = 0

    def add(cells, row_font, *, background=None, quote=0, border=False, heading=False, height=None):
        nonlocal total_height
        height = height or round(row_font.size * 1.65)
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
            if not lists:
                add([], font, height=round(font.size * .5))
        elif kind == "list_item_open":
            ordered, number = lists[-1]
            prefix = f"{number}. " if ordered else "• "
            lists[-1][1] += 1
        elif kind == "heading_open":
            heading = int(token.tag[1:])
            if rows:
                add([], font, height=round(font.size * 1.1))
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
            for row_index, (cells, header) in enumerate(table_rows):
                wrapped = [wrap(cell, font, cell_width - 16) for cell in cells]
                count = max(map(len, wrapped), default=1)
                for line in range(count):
                    add(
                        [
                            (padding + col * cell_width, cell_width, cell[line] if line < len(cell) else [])
                            for col, cell in enumerate(wrapped)
                        ],
                        font,
                        background="#e9eef5" if header else "#f6f8fa" if row_index % 2 == 0 else "#ffffff",
                        border=(line == 0, line == count - 1),
                    )
        elif kind in {"fence", "code_block"}:
            from .code_render import code_rows

            for raster in code_rows(token.content, token.info, font, width - padding * 2):
                add([(padding, width - padding * 2, raster)], font, height=raster.height)
            add([], font, height=round(font.size * .7))
        elif kind == "inline":
            scale = {1: 1.65, 2: 1.3, 3: 1.12}.get(heading, 1.0)
            row_font = font.font_variant(size=round(font.size * scale))
            indent = min(quote_depth * 16 + max(0, len(lists) - 1) * 20, (width - padding * 2) // 3)
            x = padding + indent
            runs = inline_runs(token.children)
            if heading:
                runs = [Run(run.text, True, run.italic, run.code, run.link, run.strike) for run in runs]
            marker = prefix
            prefix = ""
            body_x = x + (round(font.size * 1.25) if lists else 0)
            for line_index, line in enumerate(wrap(runs, row_font, width - padding - body_x - 16)):
                cells = [(body_x, width - padding - body_x, line)]
                if marker and line_index == 0:
                    cells.insert(0, (x, body_x - x, wrap([Run(marker)], font, 100)[0]))
                add(
                    cells,
                    row_font,
                    background="#f0f5fa" if quote_depth else None,
                    quote=quote_depth,
                    heading=heading,
                )
            if not lists:
                add([], font, height=round(font.size * 0.6))
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
    font_cache = {}
    for page_index, page in enumerate(pages):
        top = padding if page_index == 0 else 0
        bottom = padding if page_index == len(pages) - 1 else 0
        image = Image.new("RGB", (width, top + bottom + sum(row[0] for row in page)), "white")
        draw = ImageDraw.Draw(image)
        if page_index == 0:
            draw.rectangle((padding + 8, 22, padding + 52, 25), fill="#427b96")
        y = top
        for height, cells, row_font, background, quote_depth, border, is_heading in page:
            if background:
                draw.rectangle((padding, y, width - padding, y + height), fill=background)
            if quote_depth:
                draw.rounded_rectangle((padding, y + 4, padding + 4, y + height - 4), radius=2, fill="#a8b5c5")
            if border and not cells:
                draw.line((padding, y + height // 2, width - padding, y + height // 2), fill="#ccd3dd")
            for x, cell_width, glyphs in cells:
                if isinstance(glyphs, Image.Image):
                    image.paste(glyphs, (x, y))
                    continue
                if border:
                    draw.line((x, y, x, y + height), fill="#d9e1e8")
                    draw.line((x + cell_width, y, x + cell_width, y + height), fill="#d9e1e8")
                    if border[0]:
                        draw.line((x, y, x + cell_width, y), fill="#d9e1e8")
                    if border[1]:
                        draw.line((x, y + height, x + cell_width, y + height), fill="#d9e1e8")
                cursor = x + 8
                for run, group in groupby(glyphs, key=lambda item: item[1]):
                    group = list(group)
                    char = "".join(item[0] for item in group)
                    advance = sum(item[2] for item in group)
                    color = (
                        "#dce8f4"
                        if run.block_code
                        else "#2563ad"
                        if run.link
                        else "#203040"
                        if is_heading
                        else "#364152"
                    )
                    if run.code and not run.block_code:
                        draw.rounded_rectangle(
                            (cursor - 2, y + 2, cursor + advance + 2, y + height - 2), radius=4, fill="#edf2f6"
                        )
                    key = (row_font.size, run.bold)
                    if key not in font_cache:
                        selected_font = row_font
                        if run.bold:
                            try:
                                selected_font = ImageFont.truetype(
                                    str(row_font.path).replace("msyh.ttc", "msyhbd.ttc"), row_font.size
                                )
                            except OSError:
                                pass
                        font_cache[key] = selected_font
                    selected_font = font_cache[key]
                    if run.italic:
                        glyph = Image.new("RGBA", (int(advance) + height, height))
                        ImageDraw.Draw(glyph).text(
                            (height // 3, 3),
                            char,
                            font=selected_font,
                            fill=color,
                            stroke_width=0,
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
                            font=selected_font,
                            fill=color,
                            stroke_width=0,
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
