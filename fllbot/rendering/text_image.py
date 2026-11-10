"""Bounded text/Markdown images shared by tools and delivery fallback."""
from __future__ import annotations

import io
import os
from pathlib import Path

MAX_CHARS = 30_000


def validate(text: str) -> str:
    if not isinstance(text, str) or not text:
        raise ValueError("文本不能为空")
    if len(text) > MAX_CHARS:
        raise ValueError(f"文本最多 {MAX_CHARS} 个字符，请分段处理")
    return text


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
    if format == "tree":
        from .tree_render import render_tree

        return render_tree(text, font, width)
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


def join_pages(pages: list[bytes]) -> bytes:
    from PIL import Image

    if not pages:
        raise ValueError("没有可发送的图片页面")
    images = [Image.open(io.BytesIO(page)).convert("RGB") for page in pages]
    try:
        width = max(image.width for image in images)
        heights = [image.height for image in images]
        canvas = Image.new("RGB", (width, sum(heights)), "white")
        y = 0
        for image in images:
            canvas.paste(image, (0, y))
            y += image.height
        output = io.BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        return output.getvalue()
    finally:
        for image in images:
            image.close()


def render_reply_image(text: str) -> bytes:
    try:
        pages = render(text, font_size=24, format="markdown")
    except ValueError:
        pages = render(text, font_size=24, format="plain")
    return join_pages(pages)
