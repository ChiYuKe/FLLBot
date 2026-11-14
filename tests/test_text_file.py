import base64
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.builtin_plugins.text.file_export import export_text_file, text_file_name
from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot

module = importlib.import_module("fllbot.builtin_plugins.text.plugin")


@pytest.mark.parametrize("filename", ["main.py", "app.ts", "Program.cs", "config.json", "run.ps1", "notes.md"])
def test_code_and_text_extensions_are_allowed(filename):
    assert text_file_name(filename) == filename


@pytest.mark.parametrize("filename", ["main.exe", "payload.dll", "setup.bat", "run.cmd", "archive.zip", "main", ".env"])
def test_unsafe_or_unknown_extensions_are_rejected(filename):
    with pytest.raises(ValueError):
        text_file_name(filename)


def test_export_keeps_code_verbatim_and_enforces_limits():
    source = "def main():\n    print('<你好>')\n"
    name, payload = export_text_file(source, "main.py")
    assert name == "main.py" and payload.decode("utf-8") == source
    with pytest.raises(ValueError):
        export_text_file("x" * 30_001, "long.py")
    with pytest.raises(ValueError):
        export_text_file("x", "../escape.py")


def test_text_file_tools_declare_file_sending_capability():
    tools = {spec.name: spec for spec in module.plugin.ai_tools}
    for name in ("send_html_file", "send_markdown_file", "send_text_file"):
        assert tools[name].capabilities == ("qq.send_file",)
        assert tools[name].risk == "write"


async def test_send_text_file_sends_a_file_attachment(monkeypatch):
    sent = []

    async def sender(ctx, segments, content, *, require_ai=True):
        sent.append((segments, content, require_ai))
        return True

    monkeypatch.setattr(module, "send_media_reply", sender)
    result = await module.send_text_file(SimpleNamespace(), "print('hi')\n", "main.py")
    assert result["sent"] is True and result["filename"] == "main.py"
    segments, content, require_ai = sent[0]
    assert segments[0].type == "file"
    assert segments[0].data["name"] == "main.py"
    raw = str(segments[0].data["file"])
    assert raw.startswith("base64://")
    assert base64.b64decode(raw.removeprefix("base64://")).decode("utf-8") == "print('hi')\n"
    assert content == "print('hi')\n" and require_ai is True


async def test_agent_sends_generated_code_file_to_group(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake", "context_window": 8192})
    try:
        await bot.permissions.save_group(20002, enabled=True, config={"policy": {
            "auto_group": True, "trigger_modes": ["at"], "cooldown_seconds": 0,
            "allowed_tools": ["text__send_text_file"],
        }})
        complete = AsyncMock(side_effect=[
            SimpleNamespace(text="", tool_calls=[{
                "id": "file-call", "type": "function",
                "function": {"name": "text__send_text_file",
                             "arguments": json.dumps({"text": "print('hi')\n", "filename": "main.py"})},
            }]),
            SimpleNamespace(text="已发送 main.py。", tool_calls=[]),
        ])
        monkeypatch.setattr(bot.chat, "complete", complete)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(
            parse_event(group_message("把这段代码做成 main.py 发我", at=10001)), connection)
        files = [segment for _, _, segments in connection.sent for segment in segments
                 if segment.type == "file"]
        assert len(files) == 1 and files[0].data["name"] == "main.py"
        assert connection.texts()[-1] == "已发送 main.py。"
    finally:
        await bot.stop()
