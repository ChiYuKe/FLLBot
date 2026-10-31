"""Real package dispatch, hot reload rollback and offline development workflows."""

from __future__ import annotations

import asyncio
import os
import sys
from importlib.resources import files

import pytest

from fllbot.cli import main
from fllbot.onebot.models import parse_event
from fllbot.plugin.manifest import PluginManifest
from fllbot.plugin.scaffold import create_package
from helpers import FakeConnection, group_message, make_bot


async def dispatch(bot, command="/demo"):
    connection = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message(command)), connection)
    return connection.texts()


@pytest.fixture
async def package_bot(tmp_path):
    root = tmp_path / "plugins"
    create_package(root, "demo")
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[root])
    bot.cleanup_events = []
    try:
        yield bot, root / "demo"
    finally:
        await bot.stop()


@pytest.mark.parametrize("template", ["command", "tool"])
async def test_generated_package_dispatch_settings_and_metadata(tmp_path, template):
    root = tmp_path / "plugins"
    package = create_package(root, "demo", template=template, description='欢迎 "测试"', author="作者")
    assert PluginManifest.read(package).description == '欢迎 "测试"'
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[root])
    try:
        plugin = bot.plugins.get("demo")
        assert plugin is not None
        assert len(plugin.ai_tools) == (1 if template == "tool" else 0)
        assert "你好" in (await dispatch(bot))[0]
        await plugin.settings.update({"greeting": "欢迎"})
        assert "欢迎" in (await dispatch(bot))[0]
        assert plugin.data_dir == tmp_path / "data/plugins/demo"
        summary = bot.plugin_summaries()[0]
        assert summary["package"] and summary["manifest"] and summary["folder"] == "demo"
        await bot.plugins.reload("demo")
        assert "欢迎" in (await dispatch(bot))[0]
    finally:
        await bot.stop()


async def test_nested_modules_reload_even_when_same_size_and_timestamp(package_bot):
    bot, package = package_bot
    source = package / "handlers.py"
    source.write_text(
        """
from fllbot.plugin import Context, Plugin
from .helper import MESSAGE
def register_handlers(plugin):
    @plugin.on_command("demo")
    async def greet(ctx): await ctx.reply(MESSAGE)
""",
        encoding="utf-8",
    )
    helper = package / "helper.py"
    helper.write_text('MESSAGE = "one"\n', encoding="utf-8")
    await bot.plugins.reload("demo")
    assert await dispatch(bot) == ["one"]
    entry = bot.plugins.entry_for("demo")
    assert entry is not None
    original_stat = helper.stat()
    helper.write_text('MESSAGE = "two"\n', encoding="utf-8")
    os.utime(helper, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    await bot.plugins.reload("demo")
    assert await dispatch(bot) == ["two"]
    assert len(bot.dispatcher.command_handlers()) == 1
    assert sys.modules[entry.module_name] is bot.plugins.get("demo").module


async def test_manifest_and_readme_changes_trigger_reload(package_bot):
    bot, package = package_bot
    readme = package / "README.md"
    readme.write_text("# 新文档\n", encoding="utf-8")
    assert await bot.plugins.scan_once() == ["demo"]
    assert bot.plugins.get("demo").readme == "# 新文档\n"
    manifest = package / "plugin.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('version = "0.1.0"', 'version = "0.2.0"'), encoding="utf-8"
    )
    assert await bot.plugins.scan_once() == ["demo"]
    assert bot.plugins.get("demo").version == "0.2.0"


async def test_validation_failure_restores_old_modules_and_handlers(package_bot):
    bot, package = package_bot
    old = bot.plugins.get("demo")
    module_name = bot.plugins.entry_for("demo").module_name
    (package / "plugin.py").write_text("plugin = 1\n", encoding="utf-8")
    with pytest.raises(TypeError):
        await bot.plugins.reload("demo")
    assert bot.plugins.get("demo") is old
    assert sys.modules[module_name] is old.module
    assert "你好" in (await dispatch(bot))[0]
    assert str(package) in bot.plugins.failed


async def test_failed_initialization_cleans_resources_and_preserves_working_version(package_bot):
    bot, package = package_bot
    old = bot.plugins.get("demo")
    (package / "plugin.py").write_text(
        """
from fllbot.plugin import Plugin
class Broken(Plugin):
    async def on_load(self, bot):
        self.on_cleanup(lambda: bot.cleanup_events.append("cleaned"))
        raise RuntimeError("initialization failed")
plugin = Broken.from_manifest(__file__)
""",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="initialization failed"):
        await bot.plugins.reload("demo")
    assert bot.cleanup_events == ["cleaned"]
    assert bot.plugins.get("demo") is old
    assert "你好" in (await dispatch(bot))[0]


async def test_duplicate_name_from_another_source_does_not_replace_plugin(package_bot):
    bot, package = package_bot
    other = package.parent / "other.py"
    other.write_text('from fllbot.plugin import Plugin\nplugin = Plugin(name="demo")\n', encoding="utf-8")
    old = bot.plugins.get("demo")
    with pytest.raises(ValueError, match="插件名重复"):
        await bot.plugins.load_path(other)
    assert bot.plugins.get("demo") is old
    assert "你好" in (await dispatch(bot))[0]


async def test_cleanup_hooks_run_in_reverse_order_and_keep_old_relative_imports(package_bot):
    bot, package = package_bot
    implementation = package / "plugin.py"
    helper = package / "helper.py"
    helper.write_text('VALUE = "old"\n', encoding="utf-8")
    implementation.write_text(
        """
from fllbot.plugin import Plugin
class Managed(Plugin):
    async def on_load(self, bot):
        self.on_cleanup(lambda: bot.cleanup_events.append("first"))
        async def last(): bot.cleanup_events.append("last")
        self.on_cleanup(last)
    async def on_unload(self, bot):
        from .helper import VALUE
        bot.cleanup_events.append(VALUE)
plugin = Managed.from_manifest(__file__)
""",
        encoding="utf-8",
    )
    await bot.plugins.reload("demo")
    # Load the helper in the old generation before staging the next one.
    __import__(bot.plugins.entry_for("demo").module_name + ".helper")
    helper.write_text('VALUE = "new"\n', encoding="utf-8")
    await bot.plugins.reload("demo")
    assert bot.cleanup_events == ["old", "last", "first"]
    bot.cleanup_events.clear()
    await bot.plugins.unload("demo")
    assert bot.cleanup_events == ["new", "last", "first"]


def test_cli_generates_checks_and_lists_packages_without_running_code(tmp_path, capsys):
    args = ["--root", str(tmp_path)]
    assert main(["plugin", "new", "demo", *args, "--template", "tool"]) == 0
    assert main(["plugin", "check", *args, "--name", "demo"]) == 0
    assert main(["plugin", "list", *args]) == 0
    assert "demo" in capsys.readouterr().out
    assert main(["plugin", "new", "demo", *args]) == 1
    # Offline checks parse syntax but must not execute module-level side effects.
    (tmp_path / "plugins/demo/plugin.py").write_text('raise RuntimeError("must not execute")\n', encoding="utf-8")
    assert main(["plugin", "check", *args]) == 0
    (tmp_path / "plugins/demo/README.md").unlink()
    assert main(["plugin", "check", *args]) == 1


def test_init_creates_one_complete_package_and_preserves_existing_work(tmp_path):
    assert main(["init", "--root", str(tmp_path)]) == 0
    package = tmp_path / "plugins/hello"
    PluginManifest.read(package)
    assert not (tmp_path / "plugins/hello.py").exists()
    (package / "README.md").write_text("# My work", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path)]) == 0
    assert (package / "README.md").read_text(encoding="utf-8") == "# My work"


async def test_package_assets_and_periodic_tasks_are_managed(package_bot):
    bot, package = package_bot
    assets = package / "assets"
    assets.mkdir()
    (assets / "greeting.txt").write_text("package resource", encoding="utf-8")
    plugin = bot.plugins.get("demo")
    assert files(plugin.module).joinpath("assets/greeting.txt").read_text(encoding="utf-8") == "package resource"
    bot.loop_seen = asyncio.Event()
    implementation = package / "plugin.py"
    implementation.write_text(
        implementation.read_text(encoding="utf-8")
        + """
@plugin.on_loop(seconds=0.01)
async def tick(bot):
    bot.loop_seen.set()
""",
        encoding="utf-8",
    )
    await bot.plugins.reload("demo")
    await asyncio.wait_for(bot.loop_seen.wait(), timeout=1)
    tasks = list(bot.plugins.entry_for("demo").loops)
    await bot.plugins.unload("demo")
    assert tasks and all(task.done() for task in tasks)


async def test_invalid_manifest_is_reported_before_any_plugin_execution(tmp_path):
    package = create_package(tmp_path / "plugins", "demo")
    marker = tmp_path / "executed.txt"
    implementation = package / "plugin.py"
    implementation.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n", encoding="utf-8"
    )
    manifest = package / "plugin.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("schema_version = 1", "schema_version = 2"), encoding="utf-8"
    )
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[package.parent])
    try:
        assert not marker.exists()
        assert str(package) in bot.plugins.failed
        assert not bot.plugins.names()
    finally:
        await bot.stop()
