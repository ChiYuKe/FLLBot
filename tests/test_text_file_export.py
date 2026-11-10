"""Text attachments: exact UTF-8, offline HTML, real delivery guards and uncertain receipts."""

import base64
import importlib
from unittest.mock import AsyncMock

import pytest

from fllbot.builtin_plugins.text.file_export import export_file
from fllbot.onebot.connection import InvalidDeliveryReceipt
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot, private_message


def test_markdown_preserves_utf8_and_line_endings():
    text = "# 中文 😀\r\n\r\n```python\r\nprint('你好')\r\n```\n  "
    name, payload = export_file(text, "md", "说明")
    assert name == "说明.md"
    assert payload == text.encode("utf-8")


def test_html_document_formats_markdown_without_active_content():
    text = (
        '# 标题\n\n**重点**\n\n| 列 |\n| --- |\n| 值 |\n\n```python\nprint("中文")\n```\n\n'
        "<script>alert(1)</script>\n\n![外部图片](https://example.invalid/a.png)\n\n"
        "[危险链接](javascript:alert(1))"
    )
    _, payload = export_file(text, "html", "说明.html")
    result = payload.decode("utf-8")
    assert result.startswith("<!doctype html>") and '<meta charset="utf-8">' in result
    assert "<h1>标题</h1>" in result and "<strong>重点</strong>" in result and "<table>" in result
    assert '<code class="language-python">' in result
    assert "<script>" not in result and "<img" not in result and 'href="javascript:' not in result
    assert "[图片：外部图片]" in result and "Content-Security-Policy" in result
    _, plain = export_file("# <测试>", "html", "plain", format="plain")
    assert "<pre># &lt;测试&gt;</pre>" in plain.decode("utf-8")


@pytest.mark.parametrize("name", ["../test", "x/y", "x\\y", "x:ads", "CON", "a.exe", ".hidden", "x\x00", "x" * 121])
def test_bad_attachment_names_are_rejected(name):
    with pytest.raises(ValueError):
        export_file("text", "md", name)


@pytest.mark.parametrize("text", ["", "x" * 30001])
def test_attachment_input_limits(text):
    with pytest.raises(ValueError):
        export_file(text, "md", "test.md")


@pytest.mark.parametrize("kind", ["html", "md"])
@pytest.mark.parametrize("private", [False, True])
async def test_attachment_routes_to_current_chat_with_guarded_receipt(tmp_path, kind, private):
    module = importlib.import_module("fllbot.builtin_plugins.text.plugin")
    tool = module.send_html_file if kind == "html" else module.send_markdown_file
    bot = await make_bot(tmp_path, builtin=False)
    connection = FakeConnection()
    ctx = Context(bot, connection, parse_event((private_message if private else group_message)("导出文件")))
    try:
        result = await tool(ctx, "# 中文", filename="说明")
        assert result["sent"] is True and result["files"] == 1 and result["filename"] == f"说明.{kind}"
        assert len(connection.sent) == 1
        scene, target, segments = connection.sent[0]
        assert scene == ("private" if private else "group")
        assert target == (ctx.user_id if private else ctx.group_id)
        file = next(segment for segment in segments if segment.type == "file")
        assert file.data["name"] == f"说明.{kind}"
        payload = base64.b64decode(file.data["file"].removeprefix("base64://"))
        assert len(payload) == result["bytes"]
        assert (payload.decode("utf-8") == "# 中文") if kind == "md" else b"<html" in payload
        connection._closed = True
        assert (await tool(ctx, "不能发送"))["sent"] is False
        assert len(connection.sent) == 1
    finally:
        await bot.stop()


async def test_unknown_file_receipt_is_not_success_or_automatically_retried(tmp_path):
    module = importlib.import_module("fllbot.builtin_plugins.text.plugin")
    bot = await make_bot(tmp_path, builtin=False)
    connection = FakeConnection()
    connection.send_group_msg = AsyncMock(return_value={})
    ctx = Context(bot, connection, parse_event(group_message("文件")))
    try:
        with pytest.raises(InvalidDeliveryReceipt):
            await module.send_markdown_file(ctx, "正文")
        connection.send_group_msg.assert_awaited_once()
        rows = await (await bot.storage.db.execute("SELECT status FROM delivery_parts")).fetchall()
        assert [row[0] for row in rows] == ["uncertain"]
    finally:
        await bot.stop()


async def test_invalid_export_does_not_call_sender(monkeypatch):
    module = importlib.import_module("fllbot.builtin_plugins.text.plugin")
    sender = AsyncMock()
    monkeypatch.setattr(module, "send_media_reply", sender)
    assert "error" in await module.send_html_file(None, "正文", filename="../x")
    assert "error" in await module.send_markdown_file(None, "")
    sender.assert_not_called()
    for name in ("send_html_file", "send_markdown_file"):
        tool = next(spec for spec in module.plugin.ai_tools if spec.name == name)
        assert tool.effect == "side_effecting" and tool.parameters["required"] == ["text"]
