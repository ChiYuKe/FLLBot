"""插件包头契约：仅解析文件，不导入插件、不启动外部服务。"""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
REQUIRED_FILES = ("__init__.py", "plugin.py", "README.md", "CHANGELOG.md", "docs/DEVELOPMENT.md")


def validate_name(name: str) -> None:
    if not NAME_PATTERN.fullmatch(name) or name.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        raise ValueError("插件名须以小写字母开头，只含小写字母、数字、下划线，最长 64 字符，不能是系统保留名")


@dataclass(frozen=True)
class PluginManifest:
    schema_version: int
    api_version: int
    name: str
    version: str
    description: str
    author: str = ""
    category: str = "plugin"

    def describe(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def read(cls, directory: Path) -> PluginManifest:
        directory = directory.resolve()
        path = directory / "plugin.toml"
        try:
            if path.stat().st_size > 64 * 1024:
                raise ValueError("plugin.toml 不能超过 64 KiB")
            raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"无法读取插件包头 {path}: {error}") from error
        allowed = set(cls.__dataclass_fields__)
        if extra := set(raw) - allowed:
            raise ValueError(f"plugin.toml 存在未知字段：{', '.join(sorted(extra))}")
        for key in ("schema_version", "api_version"):
            if type(raw.get(key)) is not int or raw[key] != 1:
                raise ValueError(f"{key} 必须为 1")
        for key in ("name", "version", "description"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ValueError(f"plugin.toml 必须填写 {key}")
        validate_name(raw["name"])
        if directory.name != raw["name"]:
            raise ValueError("插件文件夹名必须与 plugin.toml.name 一致")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+-]+)?", raw["version"]):
            raise ValueError("version 使用三段版本号，例如 0.1.0")
        if not isinstance(raw.get("author", ""), str):
            raise ValueError("author 必须是字符串")
        if not isinstance(raw.get("category", "plugin"), str) or raw.get("category", "plugin") not in {"plugin", "tool"}:
            raise ValueError("category 必须是 plugin 或 tool，与现有权限分类一致")
        for filename in REQUIRED_FILES:
            candidate = (directory / filename).resolve()
            try:
                valid = candidate.is_relative_to(directory) and candidate.is_file() and candidate.stat().st_size > 0
            except OSError as error:
                raise ValueError(f"无法读取插件包文件：{filename}") from error
            if not valid:
                raise ValueError(f"插件包缺少非空文件：{filename}")
        return cls(**raw)
