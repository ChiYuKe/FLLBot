"""人格（系统提示词）管理：读取项目根目录 ``personas/*.md``。

所有人格都由用户自行创建，程序不内置任何人格。当默认人格未设置或指向
不存在的人格时，使用一段精简的兜底提示词，保证模型至少有基本的身份说明。
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_PERSONA_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SKIP_STEMS = {"readme", "index", "_template", "example"}

# 未配置人格时使用的兜底提示词；它不会出现在人格库中，也不可编辑。
DEFAULT_PERSONA_TEXT = (
    "你是一个运行在 QQ 里的聊天助手，名字叫 {bot_name}。\n"
    "请用自然、口语化的中文回答，避免长篇大论和机械的列表。\n"
    "遇到不确定的事情就直说不知道，不要编造事实。\n"
    "回答尽量简短，除非对方明确要求详细说明。"
)

# 人格提示词支持的变量；管理端“说明”按钮和接口共用这份清单。
PERSONA_VARIABLES: tuple[dict[str, str], ...] = (
    {"name": "bot_name", "description": "机器人当前的 QQ 昵称，取不到时回退 Bot 昵称", "example": "小助手"},
    {"name": "user_name", "description": "当前发言人的群名片或昵称；关闭“用户昵称”后为空", "example": "小明"},
    {"name": "nickname", "description": "同 user_name，保留用于旧人格", "example": "小明"},
    {"name": "user_id", "description": "当前发言人的 QQ 号；关闭“QQ 号”后为空", "example": "123456"},
    {"name": "group_id", "description": "群号；私聊时为空", "example": "20002"},
    {"name": "scene", "description": "当前场景：group（群聊）或 private（私聊）", "example": "group"},
    {"name": "date", "description": "当前日期；关闭“消息时间”后为空", "example": "2026-11-01"},
    {"name": "time", "description": "当前时间；关闭“消息时间”后为空", "example": "09:30"},
    {"name": "weekday", "description": "当前星期；关闭“消息时间”后为空", "example": "六"},
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
    revision: str = ""

    def render(self, **variables: Any) -> str:
        return render_template(self.content, variables).strip()

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title or self.name,
            "description": self.description,
            "path": str(self.path) if self.path else None,
            "preview": self.content[:200],
            "revision": self.revision,
        }


class PersonaStore:
    """从若干目录加载人格文件（``.md`` / ``.txt``）。

    目录按顺序扫描，后面的目录覆盖前面的同名人格；最后一个目录是可写的
    用户人格目录。没有内置人格目录。
    """

    def __init__(self, dirs: list[Path], *, default_name: str = "") -> None:
        self.dirs = [Path(item) for item in dirs]
        self.default_name = default_name
        self.logger = logging.getLogger("fllbot.personas")
        self._personas: dict[str, Persona] = {}
        self._fallback = Persona(name="", title="兜底提示词", description="未设置人格时使用", content=DEFAULT_PERSONA_TEXT)

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
            self.logger.info("人格库为空：请先在管理端创建人格，当前使用兜底提示词")

    def reload(self) -> None:
        self.load()

    def get(self, name: str | None) -> Persona:
        if name and name in self._personas:
            return self._personas[name]
        if self.default_name and self.default_name in self._personas:
            return self._personas[self.default_name]
        return self._fallback

    def find(self, name: str) -> Persona | None:
        return self._personas.get(name)

    def list(self) -> list[Persona]:
        return [self._personas[key] for key in sorted(self._personas)]

    def names(self) -> list[str]:
        return sorted(self._personas)

    def is_editable(self, name: str) -> bool:
        """人格是否位于可写目录；目前人格库中的条目都由用户创建。"""
        persona = self._personas.get(name)
        if not persona or not persona.path or not self.dirs:
            return False
        return persona.path.resolve().parent == self.dirs[-1].resolve()

    def save(self, name: str, title: str, description: str, content: str) -> Persona:
        """Create or update a user-managed persona in the writable directory."""
        if not _PERSONA_NAME.fullmatch(name):
            raise ValueError("人格名称只能包含英文字母、数字、下划线和连字符，且不能以符号开头")
        if name.lower() in _SKIP_STEMS:
            raise ValueError("该名称保留用于人格目录说明或示例，请使用其他标识")
        if not content.strip():
            raise ValueError("人格提示词不能为空")
        if len(title) > 100 or len(description) > 300 or len(content) > 50000:
            raise ValueError("人格名称、简介或提示词超过允许长度")
        if not self.dirs:
            raise ValueError("未配置人格目录，无法保存人格")
        directory = self.dirs[-1]
        existing = self._personas.get(name)
        target = existing.path if existing and existing.path else directory / f"{name}.md"
        if existing and not self.is_editable(name):
            raise ValueError("该人格文件位于只读目录，无法覆盖")
        if target.exists() and (not existing or not existing.path or existing.path.resolve() != target.resolve()):
            raise ValueError("同名人格文件已存在")
        clean_title = " ".join(title.splitlines()).strip() or name
        clean_description = " ".join(description.splitlines()).strip()
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".persona-{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(
                f"---\nname: {name}\ntitle: {clean_title}\ndescription: {clean_description}\n---\n\n{content.strip()}\n",
                encoding="utf-8", newline="\n",
            )
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        self.reload()
        persona = self.find(name)
        if persona is None:  # pragma: no cover - file was just written
            raise RuntimeError("保存人格后无法重新载入")
        return persona

    def delete(self, name: str) -> None:
        persona = self._personas.get(name)
        if persona is None:
            raise KeyError(name)
        if not self.is_editable(name):
            raise ValueError("该人格文件位于只读目录，无法删除")
        assert persona.path is not None
        persona.path.unlink()
        self.reload()

    @property
    def fallback(self) -> Persona:
        return self._fallback


def _parse_persona(path: Path) -> Persona:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
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

    return Persona(name=name, title=title or name, description=description, content=body.strip(), path=path,
                   revision='"'+hashlib.sha256(raw).hexdigest()+'"')


__all__ = ["DEFAULT_PERSONA_TEXT", "PERSONA_VARIABLES", "Persona", "PersonaStore", "render_template"]
