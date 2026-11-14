"""宿主维护的 AI 工具目录：插件注册一次，宿主缓存快照与模型定义。

以前每个 Agent 回合、每次工具执行前都会重新遍历插件、复制参数并构造 JSON Schema；
这里把它收敛成不可变快照：插件没有变化时复用，变化（加载、卸载、热重载、权限或
schema 修改）时按签名自动重建。Agent 插件只做“本群可见工具”的过滤，执行器只做
单工具解析，避免 O(插件×工具) 的重复工作。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def _spec_signature(spec: Any) -> tuple:
    try:
        parameters = json.dumps(spec.parameters, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - JSON Schema 应当可序列化
        parameters = repr(spec.parameters)
    digest = hashlib.sha256(parameters.encode("utf-8")).hexdigest()[:16]
    return (
        id(spec), spec.name, spec.permission, spec.effect, spec.risk,
        tuple(spec.capabilities), float(spec.timeout), int(spec.max_output_chars), digest,
    )


def _definition(spec: Any) -> dict:
    return {
        "type": "function",
        "function": {
            "name": spec.qualified_name,
            "description": spec.description,
            "parameters": copy.deepcopy(spec.parameters),
        },
    }


@dataclass(frozen=True)
class ToolEntry:
    """一个已注册工具的不可变视图；模板只读，交给模型时按需深拷贝。"""

    name: str
    plugin_name: str
    spec: Any
    owner: Any = field(repr=False)
    definition: dict = field(repr=False)

    @property
    def title(self) -> str:
        return self.spec.display_title

    @property
    def description(self) -> str:
        return self.spec.description

    def definition_copy(self) -> dict:
        return copy.deepcopy(self.definition)

    def describe(self) -> dict:
        return {
            "id": self.name,
            "title": self.title,
            "plugin": self.plugin_name,
            "description": self.description,
            "permission": self.spec.permission,
            "effect": self.spec.effect,
            "risk": self.spec.risk,
            "capabilities": list(self.spec.capabilities),
            "timeout": self.spec.timeout,
            "max_output_chars": self.spec.max_output_chars,
        }


@dataclass(frozen=True)
class ToolSnapshot:
    revision: int
    signature: str
    entries: dict[str, ToolEntry]
    plugins: tuple[str, ...]

    def get(self, name: str) -> ToolEntry | None:
        return self.entries.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self.entries)

    def by_plugin(self, plugin_name: str) -> tuple[ToolEntry, ...]:
        return tuple(entry for entry in self.entries.values() if entry.plugin_name == plugin_name)

    def describe(self) -> list[dict]:
        return [entry.describe() for entry in self.entries.values()]


class ToolRegistry:
    """按插件集合签名缓存工具快照；签名比较不执行插件代码。"""

    def __init__(self, plugins: Any) -> None:
        self._plugins = plugins
        self._snapshot: ToolSnapshot | None = None
        self._signature: tuple | None = None
        self.rebuilds = 0
        self.hits = 0

    def _signature_for(self, loaded: list[Any]) -> tuple:
        return tuple(
            (plugin.name, id(plugin), tuple(_spec_signature(spec) for spec in plugin.ai_tools))
            for plugin in sorted(loaded, key=lambda item: item.name)
        )

    def snapshot(self) -> ToolSnapshot:
        loaded = self._plugins.loaded()
        signature = self._signature_for(loaded)
        if self._snapshot is not None and signature == self._signature:
            self.hits += 1
            return self._snapshot
        entries: dict[str, ToolEntry] = {}
        for plugin in sorted(loaded, key=lambda item: item.name):
            for spec in plugin.ai_tools:
                name = spec.qualified_name
                entries[name] = ToolEntry(name=name, plugin_name=plugin.name, spec=spec,
                                          owner=plugin, definition=_definition(spec))
        self.rebuilds += 1
        digest = hashlib.sha256(repr(signature).encode("utf-8")).hexdigest()[:16]
        self._signature = signature
        self._snapshot = ToolSnapshot(
            revision=self.rebuilds,
            signature=digest,
            entries=entries,
            plugins=tuple(sorted({entry.plugin_name for entry in entries.values()})),
        )
        return self._snapshot

    def get(self, name: str) -> ToolEntry | None:
        return self.snapshot().get(name)

    def status(self) -> dict:
        snapshot = self.snapshot()
        return {
            "revision": snapshot.revision,
            "signature": snapshot.signature,
            "tools": len(snapshot.entries),
            "plugins": list(snapshot.plugins),
            "rebuilds": self.rebuilds,
            "reused": self.hits,
        }


__all__ = ["ToolEntry", "ToolRegistry", "ToolSnapshot"]
