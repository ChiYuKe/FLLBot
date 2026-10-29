"""人格（系统提示词）管理：读取 ``personas/*.md``。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_SKIP_STEMS = {"readme", "index", "_template", "example"}

DEFAULT_PERSONA_TEXT = (
    "你是一个运行在 QQ 群里的聊天助手，名字叫 {bot_name}。\n"
    "请用自然、口语化的中文回答，避免长篇大论和机械的列表。\n"
    "遇到不确定的事情就直说不知道，不要编造事实。\n"
    "回答尽量简短，除非对方明确要求详细说明。"
)


def render_template(template: str, variables: dict[str, Any]) -> str:
    """只替换已知占位符，未知的花括号原样保留。"""

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(variables[key]) if key in variables else match.group(0)

    return _PLACEHOLDER.sub(substitute, template)


@dataclass
class Persona:
    """一个可切换的人格设定。"""

    name: str
    title: str = ""
    description: str = ""
    content: str = ""
    path: Path | None = None

    def render(self, **variables: Any) -> str:
        return render_template(self.content, variables).strip()

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title or self.name,
            "description": self.description,
            "path": str(self.path) if self.path else None,
            "preview": self.content[:200],
        }


class PersonaStore:
    """从若干目录加载人格文件（``.md`` / ``.txt``）。"""

    def __init__(self, dirs: list[Path], *, default_name: str = "fllbot") -> None:
        self.dirs = [Path(item) for item in dirs]
        self.default_name = default_name
        self.logger = logging.getLogger("fllbot.personas")
        self._personas: dict[str, Persona] = {}
        self._fallback = Persona(name=default_name, title="默认人格", description="内置默认人格", content=DEFAULT_PERSONA_TEXT)

    def load(self) -> None:
        self._personas.clear()
        for directory in self.dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.suffix.lower() not in {".md", ".txt"} or not path.is_file():
                    continue
                if path.stem.lower() in _SKIP_STEMS:
                    continue
                try:
                    persona = _parse_persona(path)
                except OSError:
                    self.logger.exception("读取人格文件失败：%s", path)
                    continue
                self._personas[persona.name] = persona
        if self._personas:
            self.logger.info("已载入 %d 个人格：%s", len(self._personas), ", ".join(sorted(self._personas)))
        else:
            self.logger.info("未找到人格文件，使用内置默认人格")

    def reload(self) -> None:
        self.load()

    def get(self, name: str | None) -> Persona:
        if name and name in self._personas:
            return self._personas[name]
        if self.default_name in self._personas:
            return self._personas[self.default_name]
        return self._fallback

    def find(self, name: str) -> Persona | None:
        return self._personas.get(name)

    def list(self) -> list[Persona]:
        return [self._personas[key] for key in sorted(self._personas)]

    def names(self) -> list[str]:
        return sorted(self._personas)

    @property
    def fallback(self) -> Persona:
        return self._fallback


def _parse_persona(path: Path) -> Persona:
    text = path.read_text(encoding="utf-8")
    name = path.stem
    title = ""
    description = ""
    body = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            header, body = parts[1], parts[2]
            for line in header.splitlines():
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip()
                if key == "name" and value:
                    name = value
                elif key in {"title", "昵称"} and value:
                    title = value
                elif key in {"description", "desc", "描述"} and value:
                    description = value
            body = body.lstrip("\n")

    if not title:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                title = stripped.lstrip("#").strip()
                break

    return Persona(name=name, title=title or name, description=description, content=body.strip(), path=path)


__all__ = ["DEFAULT_PERSONA_TEXT", "Persona", "PersonaStore", "render_template"]
