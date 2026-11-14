from fllbot.policy import tool_index
from helpers import make_bot


async def test_snapshot_is_reused_and_invalidated_by_plugin_changes(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        first = bot.tools.snapshot()
        assert "workspace__read_file" in first.entries
        assert "basic__check_online" in first.entries
        assert "agent__search_history" in first.entries
        assert bot.tools.snapshot() is first
        assert bot.tools.hits >= 1

        workspace = bot.plugins.get("workspace")
        spec = workspace.ai_tools[0]
        original = spec.permission
        spec.permission = "superuser"
        try:
            changed = bot.tools.snapshot()
            assert changed is not first
            assert changed.revision == first.revision + 1
            assert changed.get(spec.qualified_name).spec is spec
        finally:
            spec.permission = original

        entry = bot.tools.snapshot().get("workspace__read_file")
        definition = entry.definition_copy()
        definition["function"]["parameters"]["properties"]["injected"] = {"type": "string"}
        assert "injected" not in entry.definition_copy()["function"]["parameters"]["properties"]

        await bot.plugins.unload("workspace")
        after = bot.tools.snapshot()
        assert "workspace__read_file" not in after.entries
        assert "basic__check_online" in after.entries
        assert after.revision > changed.revision
    finally:
        await bot.stop()


async def test_tool_index_and_status_share_the_snapshot(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        snapshot = bot.tools.snapshot()
        catalog = tool_index(bot)
        assert set(catalog) == {name for name in snapshot.entries if not name.startswith("agent__")}
        assert catalog["workspace__write_file"]["risk"] == "write"
        assert catalog["workspace__write_file"]["capabilities"] == ["fs.write"]
        status = bot.tools.status()
        assert status["tools"] == len(snapshot.entries)
        assert status["revision"] == snapshot.revision
        assert status["signature"]
    finally:
        await bot.stop()
