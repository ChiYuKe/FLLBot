"""oni_mod 插件测试：模板、参考目录、打包、真实编译与宿主沙盒裁决。"""

from __future__ import annotations

import base64
import importlib
import io
import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from fllbot.onebot.models import parse_event
from fllbot.plugin.agent import instruction_sections
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot

ROOT = Path(__file__).resolve().parents[1]
service = importlib.import_module("plugins.oni_mod.service")
module = importlib.import_module("plugins.oni_mod.plugin")


def _toolchain() -> tuple[str, Path] | None:
    try:
        return service.resolve_dotnet("dotnet"), service.resolve_game_dir("")
    except service.ModError:
        return None


TOOLCHAIN = _toolchain()
requires_toolchain = pytest.mark.skipif(
    TOOLCHAIN is None, reason="需要本机 .NET SDK 与缺氧 Managed 程序集"
)


def _files(name: str = "Sample") -> dict[str, str]:
    game = TOOLCHAIN[1] if TOOLCHAIN else Path(r"C:\Fake\Managed")
    return service.project_files(name=name, title="样例 Mod", description="测试用", game_dir=game)


def _write_all(mod_dir: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        service.write_file(mod_dir, path, content)


def test_tool_set_and_capabilities() -> None:
    tools = {spec.name: spec for spec in module.plugin.ai_tools}
    assert set(tools) == {"reference", "new", "write", "read", "build", "deliver"}
    assert tools["reference"].capabilities == ("fs.read",)
    assert tools["new"].capabilities == ("fs.write",)
    assert tools["write"].capabilities == ("fs.write",)
    assert tools["read"].capabilities == ("fs.read",)
    assert tools["build"].capabilities == ("process.exec",)
    assert tools["build"].risk == "destructive"
    assert set(tools["deliver"].capabilities) == {"fs.read", "qq.send_file"}
    assert tools["deliver"].risk == "write"


def test_agent_guidance_requires_authorized_tools() -> None:
    registry = {spec.qualified_name: (module.plugin, spec) for spec in module.plugin.ai_tools}
    definitions = [{"function": {"name": name}} for name in registry]
    sections = instruction_sections(registry, definitions)
    assert {item["source"] for item in sections} == {
        "oni_mod__oni_mod_authoring",
        "oni_mod__oni_mod_delivery",
    }
    assert instruction_sections(registry, definitions[:1]) == []


def test_project_template_has_mod_yaml_and_build_settings() -> None:
    files = _files()
    assert set(files) == {"Sample.csproj", "ModEntry.cs", "mod.yaml", "mod_info.yaml"}
    csproj = files["Sample.csproj"]
    assert "<TargetFramework>netstandard2.1</TargetFramework>" in csproj
    assert "<AssemblyName>Sample</AssemblyName>" in csproj
    assert "Assembly-CSharp.dll" in csproj and "0Harmony.dll" in csproj
    assert files["mod.yaml"] == 'title: "样例 Mod"\ndescription: "测试用"\nstaticID: "Sample"\n'
    info = files["mod_info.yaml"]
    assert "supportedContent: ALL" in info
    assert "minimumSupportedBuild: 525812" in info
    assert "version: 0.1.0" in info and "APIVersion: 2" in info
    assert "public sealed class ModEntry : UserMod2" in files["ModEntry.cs"]
    assert "harmony.PatchAll();" in files["ModEntry.cs"]


def test_extra_files_and_names_are_validated() -> None:
    game = TOOLCHAIN[1] if TOOLCHAIN else Path(r"C:\Fake\Managed")
    files = service.project_files(
        name="ExtraMod",
        title="附加源码",
        game_dir=game,
        version="1.2.3",
        static_id="ExtraMod.Stable",
        extra_files={"Patches/Foo.cs": "namespace ExtraMod { internal static class Foo { } }"},
    )
    assert files["Patches/Foo.cs"].startswith("namespace ExtraMod")
    assert 'staticID: "ExtraMod.Stable"' in files["mod.yaml"]
    with pytest.raises(service.ModError):
        service.normalize_name("../evil")
    with pytest.raises(service.ModError):
        service.normalize_version("1.0")
    with pytest.raises(service.ModError):
        service.normalize_static_id("bad id!", "Sample")
    with pytest.raises(service.ModError):
        service.validate_relative("C:/evil.cs", suffixes=service.SOURCE_SUFFIXES)
    with pytest.raises(service.ModError):
        service.validate_relative("a/../../evil.cs")
    with pytest.raises(service.ModError):
        service.validate_relative("bin/evil.cs")
    with pytest.raises(service.ModError):
        service.validate_relative("x.dll", suffixes=service.SOURCE_SUFFIXES)


def test_reference_browse_lists_reads_and_searches(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "ModA").mkdir(parents=True)
    (root / "ModA" / "ModEntry.cs").write_text("class Entry : UserMod2 { }\n", encoding="utf-8")
    (root / "ModA" / "bin").mkdir()
    (root / "ModA" / "bin" / "Hidden.cs").write_text("class Hidden { }\n", encoding="utf-8")
    (root / "README.md").write_text("# repo\n", encoding="utf-8")

    listing = service.list_reference(root, "", limit=10)
    assert {entry["name"] for entry in listing["entries"]} == {"ModA", "README.md"}
    content = service.read_reference(root, "ModA/ModEntry.cs")
    assert "UserMod2" in content["content"] and content["total_lines"] == 1
    hits = service.search_reference(root, "usermod2")
    assert [hit["file"] for hit in hits["results"]] == ["ModA/ModEntry.cs"]
    binary = root / "ModA" / "note.dll"
    binary.write_bytes(b"\x00\x01binary")
    with pytest.raises(service.ModError):
        service.read_reference(root, "ModA/note.dll")
    with pytest.raises(service.ModError):
        service.read_reference(root, "../outside.cs")


def test_package_layout_and_stale_source_detection(tmp_path: Path) -> None:
    mod_dir = tmp_path / "oni_mod" / "Sample"
    _write_all(mod_dir, _files())
    release = mod_dir / "bin" / "Release"
    release.mkdir(parents=True)
    (release / "Sample.dll").write_bytes(b"MZfake")
    built_at = time.time()
    (release / service.BUILD_MARKER).write_text(
        json.dumps({"built_at": built_at, "dll": "Sample.dll", "bytes": 6, "project": "Sample"}),
        encoding="utf-8",
    )
    state = service.build_state(mod_dir, "Sample")
    assert state["built"] is True

    filename, payload, raw = service.build_zip(mod_dir, "Sample", "0.1.0")
    assert filename == "Sample-0.1.0.zip" and raw >= 6
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert set(archive.namelist()) == {"Sample/mod.yaml", "Sample/mod_info.yaml", "Sample/Sample.dll"}

    future = time.time() + 120
    os.utime(mod_dir / "ModEntry.cs", (future, future))
    assert service.build_state(mod_dir, "Sample")["built"] is False

    # 构建记录比 dll 新（上一次成功、这一次编译失败）时，旧 dll 不能算作已编译，
    # 否则 deliver 会把上一次的产物当作本次结果发出去。
    os.utime(mod_dir / "ModEntry.cs", (built_at - 60, built_at - 60))
    (release / service.BUILD_MARKER).write_text(
        json.dumps({"built_at": time.time() + 240, "dll": "Sample.dll", "bytes": 6, "project": "Sample"}),
        encoding="utf-8",
    )
    assert service.build_state(mod_dir, "Sample")["built"] is False


@requires_toolchain
async def test_real_build_produces_dll_and_reports_errors(tmp_path: Path) -> None:
    dotnet, game = TOOLCHAIN  # type: ignore[misc]
    mod_dir = tmp_path / "oni_mod" / "BuildProbe"
    _write_all(mod_dir, _files("BuildProbe"))
    result = await service.run_build(mod_dir, "BuildProbe", dotnet=dotnet, game_dir=game, timeout=240)
    assert result["ok"] is True and result["errors"] == []
    dll = mod_dir / "bin" / "Release" / "BuildProbe.dll"
    assert dll.is_file() and dll.stat().st_size > 0
    assert service.build_state(mod_dir, "BuildProbe")["built"] is True

    service.write_file(
        mod_dir,
        "Patches/Broken.cs",
        "namespace BuildProbe { internal static class Broken { internal static void Run() { Missing(); } } }\n",
    )
    failed = await service.run_build(mod_dir, "BuildProbe", dotnet=dotnet, game_dir=game, timeout=240)
    assert failed["ok"] is False and failed["error_count"] >= 1
    assert any(item["code"].startswith("CS") for item in failed["errors"])
    assert any(item.get("file") == "Broken.cs" for item in failed["errors"])
    assert service.build_state(mod_dir, "BuildProbe")["built"] is False


@requires_toolchain
async def test_agent_tools_create_build_and_deliver(tmp_path: Path) -> None:
    dotnet, game = TOOLCHAIN  # type: ignore[misc]
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "oni_mod", plugin_dir / "oni_mod")
    bot = await make_bot(
        tmp_path,
        plugin_dirs=[plugin_dir],
        llm={"enabled": True, "api_key": "fake", "context_window": 8192},
        overrides={
            "sandbox": {
                "allow_write": True,
                "allow_process": True,
                "allow_qq_send": True,
                "destructive_requires_superuser": False,
                "max_write_calls_per_turn": 12,
            }
        },
    )
    try:
        installed = bot.plugins.get("oni_mod")
        assert installed is not None
        await installed.settings.update({"game_managed_dir": str(game), "dotnet_path": dotnet})
        await bot.permissions.save_group(
            20002,
            enabled=True,
            config={"policy": {"auto_group": True, "trigger_modes": ["at"], "cooldown_seconds": 0}},
        )
        connection = FakeConnection()
        ctx = Context(bot, connection, parse_event(group_message("帮我做个缺氧 mod", at=10001)))
        ctx.trigger_decision = SimpleNamespace(trigger_kind="at", addressed=True)
        registry = {spec.qualified_name: (installed, spec) for spec in installed.ai_tools}

        created = json.loads(
            (
                await bot.tool_executor.execute(
                    ctx, "oni_mod__new", {"name": "AgentMod", "title": "代理测试 Mod"}, registry
                )
            )[0]
        )
        assert created["project"] == "AgentMod" and created["files"]

        built = json.loads(
            (await bot.tool_executor.execute(ctx, "oni_mod__build", {"project": "AgentMod"}, registry))[0]
        )
        assert built["ok"] is True and built["dll"].endswith("AgentMod.dll")

        delivered = json.loads(
            (await bot.tool_executor.execute(ctx, "oni_mod__deliver", {"project": "AgentMod"}, registry))[0]
        )
        assert delivered["sent"] is True
        assert delivered["zip"]["sent"] is True and delivered["dll"]["sent"] is True
        attachments = [
            segment
            for _, _, segments in connection.sent
            for segment in segments
            if segment.type == "file"
        ]
        names = {segment.data["name"] for segment in attachments}
        assert {"AgentMod-0.1.0.zip", "AgentMod.dll"} <= names
        zip_segment = next(segment for segment in attachments if segment.data["name"].endswith(".zip"))
        payload = base64.b64decode(str(zip_segment.data["file"]).removeprefix("base64://"))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert set(archive.namelist()) == {
                "AgentMod/mod.yaml",
                "AgentMod/mod_info.yaml",
                "AgentMod/AgentMod.dll",
            }
    finally:
        await bot.stop()


async def test_new_is_blocked_when_sandbox_write_is_closed(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "oni_mod", plugin_dir / "oni_mod")
    bot = await make_bot(
        tmp_path,
        plugin_dirs=[plugin_dir],
        overrides={"sandbox": {"allow_write": False, "allow_process": False}},
    )
    try:
        installed = bot.plugins.get("oni_mod")
        assert installed is not None
        ctx = Context(bot, FakeConnection(), parse_event(group_message("做个 mod", at=10001)))
        ctx.trigger_decision = SimpleNamespace(trigger_kind="at", addressed=True)
        registry = {spec.qualified_name: (installed, spec) for spec in installed.ai_tools}
        output = (
            await bot.tool_executor.execute(
                ctx, "oni_mod__new", {"name": "Blocked", "title": "被拦截"}, registry
            )
        )[0]
        payload = json.loads(output)
        assert payload["error"] == "tool_blocked"
        assert payload["reason"] == "write_disabled"
        assert not (tmp_path / "data" / "agent_workspace" / "group_20002" / "oni_mod" / "Blocked").exists()
    finally:
        await bot.stop()
