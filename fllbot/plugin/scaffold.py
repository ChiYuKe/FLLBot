"""标准插件包生成与离线检查；不执行插件源码。"""

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

from .manifest import PluginManifest, validate_name

TEMPLATES = Path(__file__).with_name("templates")


def create_package(
    directory: Path, name: str, *, description: str = "请填写插件用途", author: str = "", template: str = "command"
) -> Path:
    validate_name(name)
    if template not in {"command", "tool"}:
        raise ValueError("模板必须为 command 或 tool")
    if not description.strip():
        raise ValueError("请填写插件描述")
    directory = directory.expanduser().resolve()
    target = directory / name
    if target.exists() or target.is_symlink() or (directory / f"{name}.py").exists():
        raise FileExistsError(f"插件已存在，不会覆盖：{name}")
    directory.mkdir(parents=True, exist_ok=True)
    replacements = {
        "__NAME__": name,
        "__DESCRIPTION__": description,
        "__TOOL_DOC__": "- AI 工具：`" + name + "__greet`，使用前需授予工具权限。"
        if template == "tool"
        else "- 无 AI 工具。",
        "__TOOL_HANDLER__": """
    @plugin.on_ai_tool(
        "greet", title="打招呼", description="在当前会话发送欢迎语",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def greet_tool(ctx: Context) -> dict[str, bool]:
        await greet(ctx)
        return {"ok": True}
"""
        if template == "tool"
        else "",
    }
    if not TEMPLATES.is_dir():
        raise ValueError("安装包缺少插件模板，请重新安装 FLLBot")
    with tempfile.TemporaryDirectory(prefix=".new-plugin-", dir=directory) as temporary:
        staging = Path(temporary) / name
        staging.mkdir()
        for source in TEMPLATES.rglob("*.tmpl"):
            output = staging / source.relative_to(TEMPLATES).with_suffix("")
            output.parent.mkdir(parents=True, exist_ok=True)
            content = source.read_text(encoding="utf-8")
            for key, value in replacements.items():
                content = content.replace(key, value)
            output.write_text(content, encoding="utf-8")
        # JSON string quoting is a valid TOML basic string and safely preserves names and descriptions.
        metadata = {
            "schema_version": 1,
            "api_version": 1,
            "name": name,
            "version": "0.1.0",
            "description": description,
            "author": author,
            "category": "plugin",
        }
        manifest = (
            "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items()) + "\n"
        )
        (staging / "plugin.toml").write_text(manifest, encoding="utf-8")
        check_package(staging)
        staging.rename(target)
    return target


def check_package(directory: Path) -> PluginManifest:
    manifest = PluginManifest.read(directory)
    for source in directory.rglob("*.py"):
        if "__pycache__" not in source.parts:
            ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    return manifest
