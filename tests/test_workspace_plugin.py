from types import SimpleNamespace
from unittest.mock import AsyncMock

from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot


def _call(name: str, arguments: str, call_id: str = "call-1"):
    return [{"id": call_id, "type": "function",
             "function": {"name": name, "arguments": arguments}}]


async def _prepare(tmp_path):
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake", "context_window": 8192})
    agent = bot.plugins.get("agent")
    await agent.settings.update({
        "auto_group": True, "trigger_mode": "all", "cooldown_seconds": 0,
        "participation_threshold": -100,
        "allowed_tools": ["workspace__write_file", "workspace__read_file"],
    })
    return bot


async def test_workspace_tools_load_with_capabilities(tmp_path):
    bot = await _prepare(tmp_path)
    try:
        workspace = bot.plugins.get("workspace")
        assert workspace is not None
        tools = {spec.qualified_name: spec for spec in workspace.ai_tools}
        assert set(tools) == {
            "workspace__list_files", "workspace__read_file", "workspace__write_file",
            "workspace__fetch_url", "workspace__stat", "workspace__search",
            "workspace__make_dir", "workspace__move", "workspace__copy",
            "workspace__delete", "workspace__download", "workspace__send_file",
        }
        assert tools["workspace__write_file"].risk == "write"
        assert tools["workspace__read_file"].capabilities == ("fs.read",)
        assert tools["workspace__send_file"].capabilities == ("fs.read", "qq.send_file")
        catalog = {item["name"]: item for item in bot.plugin_summaries()}
        exposed = {tool["name"]: tool for tool in catalog["workspace"]["ai_tools"]}
        assert exposed["workspace__write_file"]["risk"] == "write"
        assert exposed["workspace__write_file"]["capabilities"] == ["fs.write"]
    finally:
        await bot.stop()


async def test_automatic_participation_cannot_write_but_explicit_request_can(tmp_path, monkeypatch):
    bot = await _prepare(tmp_path)
    try:
        target = bot.sandbox.policy().workspace_for("group", 20002) / "notes" / "a.txt"
        # 写入能力已开放，但自动参与没有明确请求，仍不能产生副作用。
        await bot.update_config({"sandbox": {"allow_write": True}})
        automatic = AsyncMock(side_effect=[
            SimpleNamespace(text="", tool_calls=_call(
                "workspace__write_file", '{"path": "notes/a.txt", "content": "hello"}')),
            SimpleNamespace(text="我无法在自动参与时写文件。", tool_calls=[]),
        ])
        monkeypatch.setattr(bot.chat, "complete", automatic)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("记一下这个", message_id=1)), connection)
        assert not target.exists()
        assert bot.sandbox.stats["denied:automatic_side_effect"] == 1

        explicit = AsyncMock(side_effect=[
            SimpleNamespace(text="", tool_calls=_call(
                "workspace__write_file", '{"path": "notes/a.txt", "content": "hello"}')),
            SimpleNamespace(text="已写入。", tool_calls=[]),
        ])
        monkeypatch.setattr(bot.chat, "complete", explicit)
        await bot.dispatcher.dispatch(
            parse_event(group_message("写一个文件", message_id=2, at=10001)), FakeConnection())
        assert target.read_text(encoding="utf-8") == "hello"
        assert bot.sandbox.stats["allowed"] >= 1
    finally:
        await bot.stop()


async def test_workspace_read_blocks_escape_and_sensitive_paths(tmp_path, monkeypatch):
    bot = await _prepare(tmp_path)
    try:
        outside = tmp_path / "secret.txt"
        outside.write_text("top-secret", encoding="utf-8")
        # 沙盒工作区之外的文件即使存在也不允许通过工具读取。
        (tmp_path / "data").mkdir(exist_ok=True)
        complete = AsyncMock(side_effect=[
            SimpleNamespace(text="", tool_calls=_call(
                "workspace__read_file", '{"path": "../secret.txt"}')),
            SimpleNamespace(text="这个路径在工作区外，我无法读取。", tool_calls=[]),
        ])
        monkeypatch.setattr(bot.chat, "complete", complete)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(
            parse_event(group_message("读取 ../secret.txt", message_id=3, at=10001)), connection)
        tool_messages = [item["content"] for item in complete.call_args_list[-1].args[0]
                         if item.get("role") == "tool"]
        assert any("path_escape" in content for content in tool_messages)
        assert "top-secret" not in str(complete.call_args_list[-1].args[0])
    finally:
        await bot.stop()
