"""Plugin: text | Role: bounded local text operations | Metadata: plugin.toml."""

from __future__ import annotations

import base64
import binascii
import html
import io
import json
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote, unquote

MAX_CHARS = 30_000


def validate(text: str) -> str:
    if not isinstance(text, str) or not text:
        raise ValueError("文本不能为空")
    if len(text) > MAX_CHARS:
        raise ValueError(f"文本最多 {MAX_CHARS} 个字符，请分段处理")
    return text


def process(operation: str, text: str, **options: str) -> dict:
    validate(text)
    if operation == "statistics":
        return {
            "characters": len(text),
            "non_whitespace": sum(not c.isspace() for c in text),
            "lines": len(text.splitlines()),
            "chinese_characters": len(re.findall(r"[\u3400-\u9fff]", text)),
            "latin_words": len(re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", text)),
        }
    if operation == "transform":
        mode = options.get("mode", "trim")
        lines = text.splitlines()
        actions = {
            "trim": lambda: "\n".join(line.strip() for line in lines).strip(),
            "remove_blank_lines": lambda: "\n".join(line for line in lines if line.strip()),
            "deduplicate_lines": lambda: "\n".join(dict.fromkeys(lines)),
            "sort_lines": lambda: "\n".join(sorted(lines)),
            "lower": text.lower,
            "upper": text.upper,
            "normalize": lambda: unicodedata.normalize("NFKC", text),
        }
        if mode not in actions:
            raise ValueError("不支持的文本处理方式")
        result = actions[mode]()
    elif operation == "replace":
        old, new = options.get("old", ""), options.get("new", "")
        validate(old)
        if len(new) > MAX_CHARS:
            raise ValueError("替换内容过长")
        count = text.count(old)
        if len(text) + count * (len(new) - len(old)) > MAX_CHARS:
            raise ValueError("替换结果过长，请分段处理")
        return {"text": text.replace(old, new), "replacements": count}
    elif operation == "json":
        mode = options.get("mode", "pretty")
        if mode not in {"pretty", "compact"}:
            raise ValueError("不支持的 JSON 处理方式")
        try:
            value = json.loads(text, parse_constant=lambda _: _invalid_json())
            result = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2 if mode == "pretty" else None,
                separators=None if mode == "pretty" else (",", ":"),
            )
        except (ValueError, RecursionError) as exc:
            raise ValueError("无效的 JSON 文本") from exc
    elif operation == "codec":
        mode = options.get("mode", "")
        actions = {
            "base64_encode": lambda: base64.b64encode(text.encode()).decode("ascii"),
            "base64_decode": lambda: base64.b64decode(text, validate=True).decode("utf-8"),
            "url_encode": lambda: quote(text, safe=""),
            "url_decode": lambda: unquote(text, errors="strict"),
            "html_escape": lambda: html.escape(text),
            "html_unescape": lambda: html.unescape(text),
        }
        if mode not in actions:
            raise ValueError("不支持的编解码方式")
        try:
            result = actions[mode]()
        except (ValueError, binascii.Error, UnicodeError) as exc:
            raise ValueError("无法解码，请检查输入；Base64 解码结果须为 UTF-8 文本") from exc
    else:
        raise ValueError("不支持的操作")
    if len(result) > MAX_CHARS:
        raise ValueError("处理结果过长，请分段处理")
    return {"text": result}


def _invalid_json():
    raise ValueError("非法 JSON 常量")


def load_font(font_path: str, font_size: int):
    from PIL import ImageFont

    candidates = (
        [font_path]
        if font_path
        else [
            str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"),
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ]
    )
    font = None
    for path in candidates:
        try:
            font = ImageFont.truetype(path, font_size)
            break
        except OSError:
            continue
    if font is None:
        raise ValueError("找不到中文字体，请在 text 插件配置中填写可用的字体文件路径")
    return font


def render(
    text: str, *, font_path: str = "", font_size: int = 28, width: int = 900, format: str = "plain"
) -> list[bytes]:
    """Render local text; Markdown uses a bounded Pillow layout without a browser."""
    from PIL import Image, ImageDraw

    validate(text)
    if not 16 <= font_size <= 48 or not 400 <= width <= 1600:
        raise ValueError("字号须为 16–48，图片宽度须为 400–1600")
    font = load_font(font_path, font_size)
    if format == "markdown":
        from .markdown_render import render_markdown

        return render_markdown(text, font, width)
    if format != "plain":
        raise ValueError("不支持的渲染格式")
    padding, line_height = 32, font_size * 2
    per_page = (1800 - padding * 2) // line_height
    lines = []
    current = ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    for char in normalized:
        if char == "\n":
            lines.append(current)
            current = ""
        else:
            if current and font.getlength(current + char) > width - padding * 2:
                lines.append(current)
                current = ""
            current += char
        if len(lines) >= per_page * 20:
            raise ValueError("排版超过 20 页，请缩短文本或增大图片宽度")
    lines.append(current)
    pages = []
    for offset in range(0, len(lines), per_page):
        chunk = lines[offset : offset + per_page]
        image = Image.new("RGB", (width, padding * 2 + len(chunk) * line_height), "#ffffff")
        draw = ImageDraw.Draw(image)
        for index, line in enumerate(chunk):
            draw.text((padding, padding + index * line_height), line, font=font, fill="#202124")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        pages.append(buffer.getvalue())
    return pages
