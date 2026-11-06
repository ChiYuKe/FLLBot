"""Bounded FIFO per conversation with fair scheduling across ready conversations."""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from .conversation import ConversationKey
from .onebot.models import MessageEvent


class ConversationQueue:
    def __init__(self, capacity: int, per_conversation: int, per_group: int = 6) -> None:
        self.maxsize = capacity
        self.per_conversation = per_conversation
        self.per_group = per_group
        self._pending: dict[Any, deque] = {}
        self._ready: deque = deque()
        self._active: set[Any] = set()
        self._owners: dict[Any, Any] = {}
        self._count = 0
        self._unfinished = 0
        self._available = asyncio.Event()
        self._joined = asyncio.Event()
        self._joined.set()

    def key(self, item: tuple) -> Any:
        event, connection = item[:2]
        if isinstance(event, MessageEvent):
            return ConversationKey('qq', int(event.self_id or connection.self_id or 0), event.scene, event.scene_id,
                                   event.user_id if event.is_group else 0)
        # Protocol notices do not share message ordering or block conversations.
        return object()

    def qsize(self) -> int:
        return self._count

    def put_nowait(self, item: tuple) -> None:
        key = self.key(item)
        bucket = self._pending.get(key)
        if self._count >= self.maxsize or (bucket is not None and len(bucket) >= self.per_conversation):
            raise asyncio.QueueFull
        if bucket is None:
            bucket = self._pending[key] = deque()
        bucket.append(item)
        self._count += 1
        self._unfinished += 1
        self._joined.clear()
        if len(bucket) == 1 and key not in self._active:
            self._ready.append(key)
            self._available.set()

    async def get(self) -> tuple:
        while True:
            key = None
            for _ in range(len(self._ready)):
                candidate = self._ready.popleft()
                active = sum(isinstance(other, ConversationKey) and other.self_id == candidate.self_id
                             and other.scene == candidate.scene and other.scene_id == candidate.scene_id
                             for other in self._active) if isinstance(candidate, ConversationKey) else 0
                if not isinstance(candidate, ConversationKey) or candidate.scene != 'group' or active < self.per_group:
                    key = candidate
                    break
                self._ready.append(candidate)
            if key is not None:
                break
            self._available.clear()
            await self._available.wait()
        item = self._pending[key].popleft()
        self._count -= 1
        self._active.add(key)
        self._owners[asyncio.current_task()] = key
        return item

    def task_done(self) -> None:
        key = self._owners.pop(asyncio.current_task())
        self._active.remove(key)
        if self._pending[key]:
            self._ready.append(key)
        else:
            del self._pending[key]
        if self._ready:
            self._available.set()
        self._unfinished -= 1
        if not self._unfinished:
            self._joined.set()

    async def join(self) -> None:
        await self._joined.wait()

    def discard_pending(self) -> list[tuple]:
        items = [item for bucket in self._pending.values() for item in bucket]
        for key in list(self._pending):
            self._pending[key].clear()
            if key not in self._active:
                del self._pending[key]
        self._ready.clear()
        self._count = 0
        self._unfinished -= len(items)
        if not self._unfinished:
            self._joined.set()
        return items
