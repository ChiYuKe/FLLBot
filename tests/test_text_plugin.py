"""Text tools: local processing, image bounds, registration and media delivery."""

import base64
import importlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from fllbot.builtin_plugins.text import plugin
from fllbot.builtin_plugins.text.service import MAX_CHARS, process, render
from helpers import build_bot


def test_processing_preserves_content_and_rejects_invalid_input():
    assert process("statistics", "你好 world\n ") == {
        "characters": 10,
        "non_whitespace": 7,
        "lines": 2,
        "chinese_characters": 2,
        "latin_words": 1,
    }
    assert process("transform", "乙\n甲\n乙", mode="deduplicate_lines")["text"] == "乙\n甲"
    assert process("transform", "ＡＢＣ１２３", mode="normalize")["text"] == "ABC123"
    assert process("replace", "a.a", old=".", new="中") == {"text": "a中a", "replacements": 1}
    for text in ("{", "NaN", "[" * 1500):
        with pytest.raises(ValueError):
            process("json", text)
    assert process("json", '{"中": 1}', mode="compact")["text"] == '{"中":1}'
    for codec in ("base64", "url"):
        encoded = process("codec", "你好\n&", mode=f"{codec}_encode")["text"]
        assert process("codec", encoded, mode=f"{codec}_decode")["text"] == "你好\n&"
    with pytest.raises(ValueError):
        process("codec", "???", mode="base64_decode")
    with pytest.raises(ValueError):
        process("statistics", "x" * (MAX_CHARS + 1))
    with pytest.raises(ValueError):
        process("replace", "a" * 20000, old="a", new="aa")


def test_images_paginate_and_reject_overflow():
    pages = render("中文排版测试 ABC\n" * 70)
    assert len(pages) >= 3
    for page in pages:
        with Image.open(io.BytesIO(page)) as image:
            assert image.format == "PNG"
            assert image.width == 900 and image.height <= 1800
            assert image.getextrema()[0][0] < 100
    with pytest.raises(ValueError, match="20 页"):
        render("行\n" * 1000)
    with pytest.raises(ValueError, match="中文字体"):
        render("测试", font_path="/nonexistent-font.ttf")


def test_long_image_preserves_all_rows_at_page_boundaries():
    module = importlib.import_module("fllbot.builtin_plugins.text.plugin")
    pages = []
    for height, color in ((30, "red"), (37, "blue")):
        buffer = io.BytesIO()
        Image.new("RGB", (100, height), color).save(buffer, format="PNG")
        pages.append(buffer.getvalue())
    with Image.open(io.BytesIO(module._join_pages(pages))) as result:
        assert result.size == (100, 67)
        assert result.getpixel((50, 29)) == (255, 0, 0)
        assert result.getpixel((50, 30)) == (0, 0, 255)
        assert result.getpixel((50, 66)) == (0, 0, 255)


def test_english_identifiers_wrap_as_words():
    from fllbot.builtin_plugins.text.markdown_render import Run, wrap
    from fllbot.builtin_plugins.text.service import load_font

    font = load_font("", 24)
    lines = wrap([Run("short Implementation details")], font, int(font.getlength("Implementation") + 8))
    assert any("Implementation" in ''.join(glyph[0] for glyph in line) for line in lines)


def test_code_panel_has_syntax_colors_and_preserves_lines():
    from fllbot.builtin_plugins.text.code_render import code_rows
    from fllbot.builtin_plugins.text.service import load_font

    rows = list(code_rows('if (count > 10) {\n    return "中文";\n}', 'csharp', load_font('',24), 780))
    assert len(rows) == 5  # Language header, three original lines, bottom padding.
    colors = {color for row in rows for _,color in row.getcolors(row.width*row.height)}
    assert (203,166,247) in colors  # Keywords.
    assert (242,181,145) in colors  # Numbers.
    assert (182,216,140) in colors  # Strings, including CJK.
    assert all(row.width == 780 for row in rows)


def test_unknown_code_language_and_long_lines_are_safe():
    from fllbot.builtin_plugins.text.code_render import code_rows
    from fllbot.builtin_plugins.text.service import load_font

    rows = list(code_rows('    identifier_' * 30 + '\nend', 'not-a-real-language', load_font('',24), 400))
    assert len(rows) > 5 and all(row.width == 400 for row in rows)


def test_repeated_code_glyphs_have_identical_pixel_shape_and_baseline():
    from PIL import ImageChops

    from fllbot.builtin_plugins.text.code_render import code_rows
    from fllbot.builtin_plugins.text.service import load_font

    row = list(code_rows('x x x x x x x x', 'text', load_font('',24), 780))[1]
    ink = ImageChops.difference(row, Image.new('RGB', row.size, '#182230'))
    intervals = []
    start = None
    for x in range(65, row.width):
        filled = ink.crop((x,0,x+1,row.height)).getbbox() is not None
        if filled and start is None:
            start = x
        if not filled and start is not None:
            intervals.append((start,x))
            start = None
    # Ignore the first glyph if a platform's gutter starts slightly before x=65.
    glyphs = [ink.crop((left,0,right,row.height)) for left,right in intervals][-7:]
    assert len(glyphs) == 7
    assert all(glyph.size == glyphs[0].size and glyph.tobytes() == glyphs[0].tobytes() for glyph in glyphs)


async def test_registration_and_loading(tmp_path):
    assert not plugin.handlers
    assert len(plugin.ai_tools) == 6
    bot = build_bot(tmp_path)
    await bot.start()
    try:
        loaded = bot.plugins.get("text")
        assert loaded and not loaded.handlers
        assert bot.plugins.entry_for("text").builtin
    finally:
        await bot.stop()


async def test_render_uses_guarded_delivery(monkeypatch):
    module = importlib.import_module("fllbot.builtin_plugins.text.plugin")
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "send_media_reply", sender)
    ctx = SimpleNamespace(settings={})
    result = await module.render_image(ctx, "你好，世界！")
    assert result == {"sent": True, "images": 1, "pages_in_image": 1}
    args, kwargs = sender.call_args
    assert args[0] is ctx and args[2] == "你好，世界！"
    assert kwargs == {"require_ai": True}
    assert base64.b64decode(args[1][0].data["file"].removeprefix("base64://")).startswith(b"\x89PNG")
    sender.reset_mock()
    assert "error" in await module.render_image(ctx, "")
    sender.assert_not_called()
    sender.return_value = False
    assert await module.render_image(ctx, "已失效") == {"sent": False, "images": 0, "pages_in_image": 0}


def test_markdown_renders_styles_without_loading_resources(monkeypatch):
    import socket

    from markdown_it import MarkdownIt

    from fllbot.builtin_plugins.text.markdown_render import inline_runs

    def forbidden(*args, **kwargs):
        pytest.fail("Markdown rendering must not access the network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    sample = (
        "# 中文标题\n\n**加粗**与*斜体*、~~删除线~~、`inline code`。\n\n"
        "> 引用段落\n\n- 第一项\n- 第二项\n\n1. 有序列表\n2. 保留次序\n\n"
        "```python\nprint('你好')\n```\n\n"
        "| 名称 | 说明 |\n| --- | --- |\n| 文本 | 自动换行 |\n\n"
        "[链接文字](https://example.invalid)\n\n![图片说明](file:///missing.png)\n\n"
        "<script>alert('literal')</script>"
    )
    pages = render(sample, format="markdown")
    assert pages != render(sample, format="plain")
    for page in pages:
        with Image.open(io.BytesIO(page)) as image:
            assert image.width == 900 and image.height <= 1800
    tokens = MarkdownIt().parse("**粗体**与`代码`和![替代文字](https://example.invalid/a.png)")
    runs = inline_runs(tokens[1].children)
    assert any(run.bold and run.text == "粗体" for run in runs)
    assert any(run.code and run.text == "代码" for run in runs)
    assert any(run.text == "[图片：替代文字]" for run in runs)


def test_markdown_pagination_and_limits():
    for text in ("```\n" + "长代码行\n" * 100 + "```", "| 列一 | 列二 |\n| --- | --- |\n" + "| 内容 | 数据 |\n" * 80):
        pages = render(text, format="markdown")
        assert len(pages) > 1
        assert all(Image.open(io.BytesIO(page)).height <= 1800 for page in pages)
    with pytest.raises(ValueError, match="20 页"):
        render("段落\n\n" * 500, format="markdown")
    with pytest.raises(ValueError, match="列"):
        render("|" + "列|" * 13 + "\n|" + "---|" * 13, format="markdown")
    with pytest.raises(ValueError, match="渲染格式"):
        render("test", format="html")


async def test_image_tool_format_is_optional_and_plain_is_available(monkeypatch):
    module = importlib.import_module("fllbot.builtin_plugins.text.plugin")
    spy = Mock(return_value=[b"PNG"])
    monkeypatch.setattr(module, "render", spy)
    monkeypatch.setattr(module, "send_media_reply", AsyncMock(return_value=True))
    ctx = SimpleNamespace(settings={})
    await module.render_image(ctx, "# 标题")
    assert spy.call_args.kwargs["format"] == "markdown"
    await module.render_image(ctx, "# 标题", format="plain")
    assert spy.call_args.kwargs["format"] == "plain"
    assert module.image_parameters["required"] == ["text"]
