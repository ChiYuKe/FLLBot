from types import SimpleNamespace
from unittest.mock import AsyncMock

from fllbot.onebot.models import parse_event
from fllbot.plugin.base import AIToolSpec
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot


async def test_identical_read_only_call_reuses_result_within_turn(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        callback = AsyncMock(return_value={"value": 1})
        spec = AIToolSpec("lookup", "", {"type": "object"}, callback, plugin_name="example",
                          effect="read_only", capabilities=("system.read",))
        owner = SimpleNamespace(name="example")
        registry = {spec.qualified_name: (owner, spec)}
        ctx = Context(bot, FakeConnection(), parse_event(group_message("hello")))
        ctx.tool_call_id = "first"
        first, silent = await bot.tool_executor.execute(ctx, spec.qualified_name, {}, registry)
        ctx.tool_call_id = "second"
        second, silent2 = await bot.tool_executor.execute(ctx, spec.qualified_name, {}, registry)
        assert first == second and silent == silent2
        callback.assert_awaited_once()
        status = bot.tool_executor.status()
        assert status["cache_hits"] == 1 and status["cache_entries"] == 1
    finally:
        await bot.stop()


async def test_side_effecting_and_undeclared_tools_are_not_cached(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        owner = SimpleNamespace(name="example")
        write_callback = AsyncMock(return_value={"ok": True})
        write_spec = AIToolSpec("write", "", {"type": "object"}, write_callback,
                                plugin_name="example", effect="side_effecting",
                                capabilities=("fs.write",))
        legacy_callback = AsyncMock(return_value={"ok": True})
        legacy_spec = AIToolSpec("legacy", "", {"type": "object"}, legacy_callback,
                                 plugin_name="example", effect="read_only")
        ctx = Context(bot, FakeConnection(), parse_event(group_message("hello")))
        await bot.update_config({"sandbox": {"allow_write": True}})
        for index, spec in enumerate((write_spec, legacy_spec)):
            registry = {spec.qualified_name: (owner, spec)}
            ctx.tool_call_id = f"a{index}"
            await bot.tool_executor.execute(ctx, spec.qualified_name, {}, registry)
            ctx.tool_call_id = f"b{index}"
            await bot.tool_executor.execute(ctx, spec.qualified_name, {}, registry)
        assert write_callback.await_count == 2
        assert legacy_callback.await_count == 2
        assert bot.tool_executor.status()["cache_entries"] == 0
    finally:
        await bot.stop()
