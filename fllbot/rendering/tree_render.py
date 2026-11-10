"""Directory trees with drawn guides and independent name/description wrapping."""
from __future__ import annotations

import re

_BRANCH = re.compile(r"^([ │┃|\t]*)([├└╰╠+][─━═-]{1,3}|\|--|`--)(.*)$")


def is_tree(text: str) -> bool:
    return sum(bool(_BRANCH.match(line)) for line in text.splitlines()) >= 2


def tree_rows(text: str, font, width: int):
    from PIL import Image, ImageDraw

    from .design_theme import CARD, INK, LINE, MUTED, PAPER
    from .diagram_render import _wrap_label

    name_font = font.font_variant(size=max(16, round(font.size * .88)))
    small = font.font_variant(size=max(14, round(font.size * .72)))
    title_font = font.font_variant(size=round(font.size * 1.2))
    step = round(font.size * .8)
    line_height = round(name_font.size * 1.38)
    description_height = round(small.size * 1.5)
    lines = [line for line in text.strip().splitlines() if line.strip()]
    start = next((i for i, line in enumerate(lines) if _BRANCH.match(line.expandtabs(4))), len(lines))
    preamble = lines[:start]
    title = preamble[0].strip() if preamble else "目录结构"
    metadata = "   /   ".join(line.strip() for line in preamble[1:])
    titles = _wrap_label(title, title_font, width - 80, max_lines=100)
    metadata_lines = _wrap_label(metadata, small, width - 80, max_lines=100) if metadata else []
    header_height = 68 + len(titles) * round(title_font.size * 1.4) + len(metadata_lines) * description_height
    header = Image.new("RGB", (width, header_height), PAPER)
    draw = ImageDraw.Draw(header)
    ty = 24
    for line in titles:
        draw.text((40, ty), line, font=title_font, fill=INK, stroke_width=1)
        ty += round(title_font.size * 1.4)
    for line in metadata_lines:
        draw.text((40, ty + 8), line, font=small, fill=MUTED)
        ty += description_height
    draw.line((40, header.height - 14, width - 40, header.height - 14), fill=LINE)
    yield header
    entries = lines[start:]
    for index, raw in enumerate(entries):
        match = _BRANCH.match(raw.expandtabs(4))
        if match:
            prefix, branch, content = match.groups()
            depth = len(prefix) // 4
            parts = re.split(r"\s{2,}|\s+#\s*", content.strip(), maxsplit=1)
            name = parts[0]
            description = parts[1] if len(parts) > 1 else ""
        else:
            prefix, branch, depth = "", "", 0
            name, description = raw.strip(), ""
        is_folder = name.rstrip().endswith("/")
        section = depth == 0
        next_match = _BRANCH.match(entries[index + 1].expandtabs(4)) if index + 1 < len(entries) else None
        ends_section = next_match is None or not next_match.group(1)
        inset = min(depth, max(1, (width - 240) // step)) * step
        guide_x = 53 + inset
        name_x = guide_x + 44
        available = width - name_x - 48
        split = bool(description) and width >= 720 and available >= font.size * 16
        desc_x = max(round(width * .56), name_x + font.size * 5) if split else name_x
        name_width = desc_x - name_x - 24 if split else available
        names = _wrap_label(name, name_font, name_width, max_lines=100)
        descriptions = _wrap_label(description, small, width - desc_x - 48, max_lines=100) if description else []
        name_height = len(names) * line_height
        desc_height = len(descriptions) * description_height
        top = 10 if section else 6
        height = top + 8 + (max(name_height, desc_height) if split else name_height + desc_height)
        if ends_section:
            height += 4
        row = Image.new("RGB", (width, height), PAPER)
        draw = ImageDraw.Draw(row)
        draw.rectangle((28, 0, width - 28, height), fill=CARD)
        if section and is_folder:
            draw.rectangle((29, 0, width - 29, height), fill="#f0f0e8")
            draw.line((29, 0, width - 29, 0), fill=LINE)
        if depth:
            for level in range(depth):
                if any(c in "│┃|" for c in prefix[level * 4:(level + 1) * 4]):
                    gx = 53 + level * step
                    draw.line((gx, 0, gx, height), fill=LINE, width=1)
            middle = top + line_height // 2
            draw.line((guide_x, 0, guide_x, middle if branch[0] in "└╰`" else height), fill=LINE)
            draw.line((guide_x, middle, guide_x + 12, middle), fill=LINE)
        icon_x, icon_y = guide_x + 17, top + 7
        if is_folder:
            draw.rounded_rectangle((icon_x, icon_y + 3, icon_x + 17, icon_y + 15), radius=3,
                                   fill="#e9dfc9", outline="#a88f5c")
            draw.rectangle((icon_x + 1, icon_y, icon_x + 8, icon_y + 4), fill="#e9dfc9")
        else:
            draw.rounded_rectangle((icon_x + 2, icon_y, icon_x + 14, icon_y + 16), radius=2, outline="#a9ada2")
            draw.line((icon_x + 5, icon_y + 6, icon_x + 11, icon_y + 6), fill="#a9ada2")
        for i, line in enumerate(names):
            draw.text((name_x, top + i * line_height - 2), line, font=name_font,
                      fill=INK, stroke_width=0)
        for i, line in enumerate(descriptions):
            draw.text((desc_x, top + 1 + i * description_height + (0 if split else name_height)),
                      line, font=small, fill=MUTED)
        yield row


def render_tree(text: str, font, width: int) -> list[bytes]:
    import io

    from PIL import Image

    pages, rows, height = [], [], 0

    def finish(final=False):
        top = 0 if pages else 16
        image = Image.new("RGB", (width, height + top + (16 if final else 0)), "#f7f5ef")
        y = top
        for row in rows:
            image.paste(row, (0, y))
            y += row.height
        output = io.BytesIO()
        image.save(output, format="PNG")
        pages.append(output.getvalue())

    for row in tree_rows(text, font, width):
        if height + row.height > 1768 and rows:
            finish()
            rows, height = [], 0
        if row.height > 1768 or len(pages) >= 20:
            raise ValueError("结构树排版过长，请缩短说明或拆分子树")
        rows.append(row)
        height += row.height
    if rows:
        finish(final=True)
    return pages
