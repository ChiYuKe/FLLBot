"""Task-local immutable inputs for one model execution; authorization remains live."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExecutionConfig:
    owner: int
    config: Any
    scene: str
    scene_id: int
    group_config: dict
    policy: dict
    defaults: Any
    personas: Any
    agent: dict = field(default_factory=dict)

    @property
    def version(self) -> str:
        effective = {'config':self.config.model_dump(mode='json'),'group':self.group_config,
                     'policy':self.policy,'defaults':self.defaults.model_dump(mode='json'),'agent':self.agent}
        return hashlib.sha256(json.dumps(effective,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


_active: ContextVar[ExecutionConfig | None] = ContextVar('model_execution_config', default=None)


def current(owner: Any) -> ExecutionConfig | None:
    value = _active.get()
    return value if value is not None and value.owner == id(owner) else None


@contextmanager
def execution_scope(value: ExecutionConfig):
    token = _active.set(value)
    try:
        yield value
    finally:
        _active.reset(token)
