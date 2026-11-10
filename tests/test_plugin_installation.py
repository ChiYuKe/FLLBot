"""Third-party package distribution, failed upgrades, recovery and runtime exclusion."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from fllbot.cli import main
from fllbot.onebot.models import parse_event
from fllbot.plugin.manifest import PluginManifest
from fllbot.plugin.package_lock import PackageLock
from fllbot.plugin.packages import PackageStore
from fllbot.plugin.scaffold import create_package
from helpers import FakeConnection, group_message, make_bot


@pytest.fixture
def distribution(tmp_path):
    package = create_package(tmp_path / "author", "demo", template="tool")
    (package / "assets").mkdir()
    (package / "assets" / "hello.txt").write_text("来自第三方包", encoding="utf-8")
    (package / "handlers.py").write_text(
        """
from importlib.resources import files
def register_handlers(plugin):
    @plugin.on_command("demo")
    async def greet(ctx):
        await ctx.reply(files(__package__).joinpath("assets/hello.txt").read_text(encoding="utf-8"))
""",
        encoding="utf-8",
    )
    return package


def set_version(package, version):
    manifest = package / "plugin.toml"
    text = manifest.read_text(encoding="utf-8")
    current = PluginManifest.read(package).version
    manifest.write_text(text.replace(f'version = "{current}"', f'version = "{version}"'), encoding="utf-8")


async def test_external_package_installs_loads_resources_and_preserves_data(tmp_path, distribution):
    root = tmp_path / "host"
    store = PackageStore(root / "plugins")
    # A downloaded checkout does not have to be named after its manifest.
    source = distribution.rename(distribution.with_name("third-party-main"))
    installed = store.install(source)
    shutil.rmtree(source)
    bot = await make_bot(root, builtin=False)
    try:
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("/demo")), connection)
        assert connection.texts() == ["来自第三方包"]
        plugin = bot.plugins.get("demo")
        await plugin.settings.update({"greeting": "saved"})
        plugin.data_dir.mkdir(parents=True, exist_ok=True)
        data = plugin.data_dir / "record.txt"
        data.write_text("keep", encoding="utf-8")
        with pytest.raises(ValueError, match="正在使用"):
            store.remove("demo")
        assert installed.exists()
    finally:
        await bot.stop()
    store.remove("demo")
    assert not installed.exists()
    assert data.read_text(encoding="utf-8") == "keep"
    store.rollback("demo")
    bot = await make_bot(root, builtin=False)
    try:
        assert bot.plugins.get("demo").settings.get("greeting") == "saved"
    finally:
        await bot.stop()


@pytest.mark.parametrize("wrapped", [False, True])
def test_zip_install_upgrade_and_rollback(tmp_path, distribution, wrapped):
    archive = tmp_path / "plugin.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for path in distribution.rglob("*"):
            if path.is_file():
                name = path.relative_to(distribution)
                output.write(path, f"download-main/{name.as_posix()}" if wrapped else name.as_posix())
    store = PackageStore(tmp_path / "host/plugins")
    installed = store.install(archive)
    set_version(distribution, "0.2.0")
    (distribution / "assets/hello.txt").write_text("new", encoding="utf-8")
    store.install(distribution, upgrade=True)
    assert PluginManifest.read(installed).version == "0.2.0"
    store.rollback("demo")
    assert PluginManifest.read(installed).version == "0.1.0"
    assert (installed / "assets/hello.txt").read_text(encoding="utf-8") == "来自第三方包"


@pytest.mark.parametrize("failure", ["syntax", "dependency", "host", "api"])
def test_bad_upgrade_keeps_working_directory_and_previous_backup(tmp_path, distribution, failure):
    store = PackageStore(tmp_path / "plugins")
    installed = store.install(distribution)
    set_version(distribution, "0.2.0")
    store.install(distribution, upgrade=True)
    set_version(distribution, "0.3.0")
    manifest = distribution / "plugin.toml"
    if failure == "syntax":
        (distribution / "plugin.py").write_text("this is !invalid", encoding="utf-8")
    else:
        text = manifest.read_text(encoding="utf-8")
        text = (
            text.replace("dependencies = []", 'dependencies = ["fllbot-impossible-test-dependency>=1"]')
            if failure == "dependency"
            else text
        )
        text = text.replace('requires_fllbot = ">=0.2,<1"', 'requires_fllbot = ">=999"') if failure == "host" else text
        text = text.replace("api_version = 1", "api_version = 999") if failure == "api" else text
        manifest.write_text(text, encoding="utf-8")
    with pytest.raises((ValueError, SyntaxError)):
        store.install(distribution, upgrade=True)
    assert PluginManifest.read(installed).version == "0.2.0"
    store.rollback("demo")
    assert PluginManifest.read(installed).version == "0.1.0"


@pytest.mark.parametrize("filename", ["../escape", "/absolute", "C:/escape", "x\\escape", "demo./x"])
def test_zip_path_rejection(tmp_path, filename):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(filename, "no")
    with pytest.raises(ValueError):
        PackageStore(tmp_path / "plugins").install(archive)
    assert not (tmp_path / "escape").exists()


def test_cli_lifecycle_and_exclusion(tmp_path, distribution, capsys):
    options = ["--root", str(tmp_path / "host")]
    assert main(["plugin", "install", str(distribution), *options]) == 0
    assert main(["plugin", "install", str(distribution), *options]) == 1
    assert main(["plugin", "dependencies", "demo", *options]) == 0
    with PackageLock(tmp_path / "host/plugins"):
        assert main(["plugin", "remove", "demo", *options]) == 1
    assert main(["plugin", "remove", "demo", *options]) == 0
    assert main(["plugin", "rollback", "demo", *options]) == 0
    assert "正在使用" in capsys.readouterr().err


async def test_incompatible_plugin_is_rejected_before_import(tmp_path, distribution):
    package = tmp_path / "plugins/demo"
    package.parent.mkdir()
    shutil.copytree(distribution, package)
    manifest = package / "plugin.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('requires_python = ">=3.11"', 'requires_python = ">=999"'),
        encoding="utf-8",
    )
    (package / "__init__.py").write_text('raise AssertionError("must not execute")', encoding="utf-8")
    bot = await make_bot(tmp_path, builtin=False)
    try:
        assert bot.plugins.get("demo") is None
        assert "Python>=999" in bot.plugins.failed[str(package)]
    finally:
        await bot.stop()


@pytest.mark.parametrize("statement", ["from .. import other", "import plugins.other", "from fllbot.app import FLLBot"])
def test_package_cannot_depend_on_external_implementation(tmp_path, distribution, statement):
    (distribution / "private.py").write_text(statement, encoding="utf-8")
    with pytest.raises(ValueError, match="导入"):
        PackageStore(tmp_path / "plugins").install(distribution)
    assert not (tmp_path / "plugins/demo").exists()


@pytest.mark.parametrize("failure", ["replace", "backup"])
def test_file_replacement_failure_restores_original_and_backup(tmp_path, distribution, monkeypatch, failure):
    store = PackageStore(tmp_path / "plugins")
    installed = store.install(distribution)
    set_version(distribution, "0.2.0")
    store.install(distribution, upgrade=True)
    set_version(distribution, "0.3.0")
    original = Path.rename

    def fail_once(path, target):
        if (failure == "replace" and path.parent.name == "ready") or (failure == "backup" and path.name == "saved"):
            monkeypatch.setattr(Path, "rename", original)
            raise OSError("simulated file replacement failure")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", fail_once)
    with pytest.raises(OSError, match="simulated"):
        store.install(distribution, upgrade=True)
    assert PluginManifest.read(installed).version == "0.2.0"
    store.rollback("demo")
    assert PluginManifest.read(installed).version == "0.1.0"


def test_dependency_versions_and_environment_markers(distribution):
    manifest = distribution / "plugin.toml"
    original = manifest.read_text(encoding="utf-8")
    manifest.write_text(original.replace("dependencies = []", 'dependencies = ["httpx>=999"]'), encoding="utf-8")
    with pytest.raises(ValueError, match="已安装"):
        PluginManifest.read(distribution).check_environment()
    manifest.write_text(
        original.replace("dependencies = []", """dependencies = ["nonexistent; python_version < '2'"]"""),
        encoding="utf-8",
    )
    PluginManifest.read(distribution).check_environment()


def test_repeated_io_failure_preserves_old_package_for_manual_recovery(tmp_path, distribution, monkeypatch):
    directory = tmp_path / "plugins"
    store = PackageStore(directory)
    store.install(distribution)
    set_version(distribution, "0.2.0")
    original = Path.rename

    def blocked(path, target):
        if path.parent.name == "ready" or path.name == "saved":
            raise OSError("disk unavailable")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", blocked)
    with pytest.raises(OSError, match="原包已保留"):
        store.install(distribution, upgrade=True)
    saved = list(directory.glob(".install-*/saved/plugin.toml"))
    assert len(saved) == 1
    assert 'version = "0.1.0"' in saved[0].read_text(encoding="utf-8")
