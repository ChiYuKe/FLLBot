"""事件分发测试：优先级、阻断、权限、开关、限流。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot

PLUGIN_SRC = '''
from fllbot.plugin import Context, Plugin, keyword

plugin = Plugin(name="tester", version="0.1.0", description="测试插件")


@plugin.on_message(rule=keyword("order"), priority=10)
async def order_a(ctx: Context) -> None:
    await ctx.reply("a")


@plugin.on_message(rule=keyword("order"), priority=20)
async def order_b(ctx: Context) -> None:
    await ctx.reply("b")


@plugin.on_message(rule=keyword("blockme"), priority=10, block=True)
async def block_first(ctx: Context) -> None:
    await ctx.reply("blocked-first")


@plugin.on_message(rule=keyword("blockme"), priority=20)
async def block_second(ctx: Context) -> None:
    await ctx.reply("blocked-second")


@plugin.on_command("secret", priority=5, permission="superuser")
async def secret(ctx: Context) -> None:
    await ctx.reply("secret")


@plugin.on_command("slow", priority=5, cooldown=60.0)
async def slow(ctx: Context) -> None:
    await ctx.reply("slow")


@plugin.on_message(priority=90)
async def fallback(ctx: Context) -> None:
    await ctx.reply("fallback")
'''


@pytest.fixture
async def dbot(tmp_path: Path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "tester.py").write_text(PLUGIN_SRC, encoding="utf-8")
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[plugin_dir])
    try:
        yield bot
    finally:
        await bot.stop()


async def dispatch(bot, payload: dict, connection: FakeConnection) -> FakeConnection:
    await bot.dispatcher.dispatch(parse_event(payload), connection)  # type: ignore[arg-type]
    return connection


async def test_priority_order(dbot) -> None:
    conn = await dispatch(dbot, group_message("order please"), FakeConnection())
    # a、b 按优先级先后回复，最后的兜底处理器也仍然会执行
    assert conn.texts() == ["a", "b", "fallback"]


async def test_block_stops_later_handlers(dbot) -> None:
    conn = await dispatch(dbot, group_message("blockme"), FakeConnection())
    assert conn.texts() == ["blocked-first"]


async def test_command_blocks_fallback(dbot) -> None:
    conn = await dispatch(dbot, group_message("/secret", user_id=99999), FakeConnection())
    assert conn.texts() == ["secret"]


async def test_permission_denied_falls_through(dbot) -> None:
    conn = await dispatch(dbot, group_message("/secret", user_id=30003), FakeConnection())
    assert "secret" not in conn.texts()
    assert conn.texts() == ["fallback"]


async def test_plugin_disabled(dbot) -> None:
    await dbot.permissions.set_plugin_enabled("tester", False)
    conn = await dispatch(dbot, group_message("order"), FakeConnection())
    assert conn.texts() == []


async def test_group_disabled(dbot) -> None:
    await dbot.permissions.set_group_enabled(20002, False)
    conn = await dispatch(dbot, group_message("order"), FakeConnection())
    assert conn.texts() == []


async def test_private_allowed(dbot) -> None:
    from helpers import private_message

    conn = await dispatch(dbot, private_message("/secret", user_id=99999), FakeConnection())
    assert conn.texts() == ["secret"]


async def test_cooldown(dbot) -> None:
    first = await dispatch(dbot, group_message("/slow"), FakeConnection())
    second = await dispatch(dbot, group_message("/slow"), FakeConnection())
    assert first.texts() == ["slow"]
    # 命令被限流后，兜底处理器依然会响应
    assert "slow" not in second.texts()


async def test_builtin_ping_plugin(bot) -> None:
    conn = await dispatch(bot, group_message("/ping"), FakeConnection())
    assert conn.texts() == ["pong"]

    conn = await dispatch(bot, group_message("ping", at=10001), FakeConnection())
    assert conn.texts() == ["pong"]


async def test_builtin_help_plugin(bot) -> None:
    conn = await dispatch(bot, group_message("/help"), FakeConnection())
    text = conn.last_text()
    assert "可用命令" in text
    assert "/ping" in text
    assert "/echo" in text
