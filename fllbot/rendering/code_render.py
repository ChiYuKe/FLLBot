"""Shared local code render implementation."""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path


def code_rows(source: str, language: str, body_font, width: int):
    from PIL import Image, ImageDraw, ImageFont
    from pygments import lex
    from pygments.lexers import TextLexer, get_lexer_by_name
    from pygments.token import Comment, Keyword, Name, Number, String
    from pygments.util import ClassNotFound

    size = max(16, round(body_font.size * 0.9))
    mono = body_font.font_variant(size=size)
    for path in (
        str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/consola.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ):
        try:
            mono = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    cjk = body_font.font_variant(size=size)
    cell = round(max(mono.getlength("M"), cjk.getlength("中") / 2))
    ascent = max(mono.getmetrics()[0], cjk.getmetrics()[0])
    descent = max(mono.getmetrics()[1], cjk.getmetrics()[1])
    height = max(round(size * 1.65), ascent + descent + 6)
    baseline = (height - ascent - descent) // 2 + ascent
    language = language.strip().split()[0][:64] if language.strip() else ""
    try:
        lexer = get_lexer_by_name(language, stripnl=False, ensurenl=False)
    except ClassNotFound:
        lexer = TextLexer(stripnl=False, ensurenl=False)
    source = source.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n").expandtabs(4)
    total_lines = source.count("\n") + 1
    gutter = max(40, round(cell * (len(str(total_lines)) + 2)))
    left = gutter + 20
    columns = max(8, int((width - left - 20) / cell))
    background, foreground = "#182230", "#dce5ef"

    def color(token):
        if token in Comment:
            return "#91a2b8"
        if token in Keyword:
            return "#cba6f7"
        if token in String:
            return "#b6d88c"
        if token in Number:
            return "#f2b591"
        if token in Name.Function or token in Name.Class:
            return "#8bc9ed"
        return foreground

    header = Image.new("RGB", (width, height + 12), "#202e40")
    ImageDraw.Draw(header).text((18, 6), lexer.name, font=cjk, fill="#a8bed5")
    yield header
    line_number, column = 1, 0
    indent, leading = 0, True
    continuation = False
    row = Image.new("RGB", (width, height), background)

    def finish():
        draw = ImageDraw.Draw(row)
        label = ">" if continuation else str(line_number)
        draw.text((gutter, baseline), label, font=mono, fill="#73869d", anchor="rs")
        draw.line((gutter + 9, 0, gutter + 9, height), fill="#2b394c")
        return row

    glyphs = {}
    for token, value in lex(source, lexer):
        for piece in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|.", value, flags=re.S):
            if (
                len(piece) > 1
                and len(piece) <= columns - min(indent + 2, columns // 3)
                and column + len(piece) > columns
            ):
                yield finish()
                row = Image.new("RGB", (width, height), background)
                continuation = True
                column = min(indent + 2, columns // 3)
            for char in piece:
                if char == "\n":
                    yield finish()
                    row = Image.new("RGB", (width, height), background)
                    line_number += 1
                    column, indent, leading, continuation = 0, 0, True, False
                    continue
                units = (
                    0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
                )
                if column + units > columns:
                    yield finish()
                    row = Image.new("RGB", (width, height), background)
                    continuation = True
                    column = min(indent + 2, columns // 3)
                selected = mono if ord(char) < 128 else cjk
                # Rasterize each glyph once at a fixed pixel phase and baseline.
                # Reusing tiles prevents fractional advances from changing the
                # antialiasing/position of the same character across columns.
                key = (char, color(token))
                if key not in glyphs:
                    tile = Image.new("RGBA", (cell * max(1, units), height))
                    x = round((tile.width - selected.getlength(char)) / 2)
                    ImageDraw.Draw(tile).text((x, baseline), char, font=selected, fill=key[1], anchor="ls")
                    glyphs[key] = tile
                tile = glyphs[key]
                x = left + max(0, column - 1 if units == 0 else column) * cell
                row.paste(tile, (x, 0), tile)
                column += units
                if leading and char == " ":
                    indent += 1
                else:
                    leading = False
    yield finish()
    yield Image.new("RGB", (width, 14), background)
