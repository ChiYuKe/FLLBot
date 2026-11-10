"""Immutable plugin contributions to prompts and the host tool pipeline."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


def validate_name(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name):
        raise ValueError('扩展名称必须是小写字母开头的标识符，最多 64 字符')


@dataclass(frozen=True)
class AgentInstruction:
    name: str
    tools: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class ToolHook:
    name: str
    stage: Literal['before', 'after', 'completed']
    tools: tuple[str, ...]
    callback: Callable
    timeout: float


@dataclass(frozen=True)
class ToolEvent:
    """No mutable Context, callback arguments or connection is exposed to hooks."""
    tool: str
    turn_id: str
    call_id: str
    execution_id: str = ''
    status: str = 'pending'
    arguments: str = '{}'
    content: str = ''
    silent: bool = False


@dataclass(frozen=True)
class ToolRejection:
    reason: str


@dataclass(frozen=True)
class ToolContent:
    """Replacement model-facing content; cannot change the execution fact or silence."""
    content: str


def instruction_sections(registry: dict, available: list[dict]) -> list[dict[str, str]]:
    """Select from the same permission-filtered registry used for this request."""
    names = {item['function']['name'] for item in available}
    owners = {owner.name: owner for name, (owner, _) in registry.items() if name in names}
    sections = []
    for name, owner in sorted(owners.items()):
        for spec in sorted(getattr(owner, 'agent_instructions', ()), key=lambda item: item.name):
            if all(f'{name}__{tool}' in names for tool in spec.tools):
                sections.append({'source': f'{name}__{spec.name}', 'text': spec.text})
    return sections
