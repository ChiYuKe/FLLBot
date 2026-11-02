"""Task-owned reentrant lock for settings mutations sharing one storage instance."""

from __future__ import annotations

import asyncio
from types import TracebackType


class SettingsLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def __aenter__(self) -> SettingsLock:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Settings mutations require an asyncio task")
        if self._owner is not task:
            await self._lock.acquire()
            self._owner = task
        self._depth += 1
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None,
    ) -> None:
        if self._owner is not asyncio.current_task():
            raise RuntimeError("Settings lock can only be released by its owner")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()
