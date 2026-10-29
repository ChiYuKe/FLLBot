"""插件加载、卸载与热重载测试。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot

PLUGIN_V1 = '''
from fllbot.plugin import Context, Plugin

plugin = Plugin(name="demo", version="0.1.0", description="演示插件")


@plugin.on_command("demo", priority=5)
async def demo(ctx: Context) -> None:
    await ctx.reply("v1")
'''

PLUGIN_V2 = PLUGIN_V1.replace('"v1"', '"v2"')

BAD_PLUGIN = "value = 1\n"


@pytest.fixture
async def pbot(tmp_path: Path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[plugin_dir])
    try:
        yield bot, plugin_dir
    finally:
        await bot.stop()


async def run_command(bot, command: str = "/demo") -> list[str]:
    conn = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message(command)), conn)  # type: ignore[arg-type]
    return conn.texts()


async def test_auto_load_from_dir(pbot) -> None:
    bot, plugin_dir = pbot
    (plugin_dir / "demo.py").write_text(PLUGIN_V1, encoding="utf-8")
    await bot.plugins.load_path(plugin_dir / "demo.py")

    assert bot.plugins.get("demo") is not None
    assert await run_command(bot) == ["v1"]
    assert any(item["name"] == "demo" for item in bot.plugin_summaries())


async def test_unload_removes_handlers(pbot) -> None:
    bot, plugin_dir = pbot
    (plugin_dir / "demo.py").write_text(PLUGIN_V1, encoding="utf-8")
    await bot.plugins.load_path(plugin_dir / "demo.py")
    assert await bot.plugins.unload("demo") is True

    assert bot.plugins.get("demo") is None
    assert await run_command(bot) == []
    assert await bot.plugins.unload("demo") is False


async def test_reload_picks_up_changes(pbot) -> None:
    bot, plugin_dir = pbot
    path = plugin_dir / "demo.py"
    path.write_text(PLUGIN_V1, encoding="utf-8")
    await bot.plugins.load_path(path)
    assert await run_command(bot) == ["v1"]

    path.write_text(PLUGIN_V2, encoding="utf-8")
    await bot.plugins.reload("demo")
    assert await run_command(bot) == ["v2"]


async def test_watcher_detects_change(pbot) -> None:
    bot, plugin_dir = pbot
    path = plugin_dir / "demo.py"
    path.write_text(PLUGIN_V1, encoding="utf-8")
    await bot.plugins.load_path(path)

    await bot.plugins.scan_once()  # 记录基线

    path.write_text(PLUGIN_V2, encoding="utf-8")
    future = time.time() + 10
    os.utime(path, (future, future))

    changed = await bot.plugins.scan_once()
    assert "demo" in changed
    assert await run_command(bot) == ["v2"]


async def test_watcher_detects_new_file(pbot) -> None:
    bot, plugin_dir = pbot
    await bot.plugins.scan_once()

    (plugin_dir / "fresh.py").write_text(PLUGIN_V1.replace('"demo"', '"fresh"'), encoding="utf-8")
    changed = await bot.plugins.scan_once()
    assert "fresh" in changed
    assert bot.plugins.get("fresh") is not None
    assert "fresh" in bot.plugins.names()


async def test_watcher_unloads_removed_file(pbot) -> None:
    bot, plugin_dir = pbot
    path = plugin_dir / "demo.py"
    path.write_text(PLUGIN_V1, encoding="utf-8")
    await bot.plugins.load_path(path)
    await bot.plugins.scan_once()

    path.unlink()
    changed = await bot.plugins.scan_once()
    assert "demo" in changed
    assert bot.plugins.get("demo") is None


async def test_bad_plugin_does_not_crash_startup(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "broken.py").write_text(BAD_PLUGIN, encoding="utf-8")
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[plugin_dir])
    try:
        assert str(plugin_dir / "broken.py") in bot.plugins.failed
        assert bot.plugins.names() == []
    finally:
        await bot.stop()


async def test_package_plugin(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins"
    package = plugin_dir / "packaged"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(PLUGIN_V1.replace('"demo"', '"packaged"').replace('"demo"', '"packaged"'), encoding="utf-8")

    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[plugin_dir])
    try:
        plugin = bot.plugins.get("packaged")
        assert plugin is not None
        assert plugin.handlers
    finally:
        await bot.stop()
