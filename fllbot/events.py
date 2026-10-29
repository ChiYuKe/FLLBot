"""管理端事件推流：SSE 订阅中心 + 事件去重。"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any


class DedupCache:
    """短时间窗口去重，防止 NapCat 重连时重放同一条消息。"""

    def __init__(self, size: int = 512, ttl: float = 120.0) -> None:
        self._size = max(1, int(size))
        self._ttl = max(0.0, float(ttl))
        self._items: OrderedDict[str, float] = OrderedDict()

    def seen(self, key: str, now: float | None = None) -> bool:
        """返回 ``True`` 表示最近已经处理过同一个 key。"""
        current = time.monotonic() if now is None else now
        previous = self._items.get(key)
        if previous is not None and current - previous <= self._ttl:
            self._items.move_to_end(key)
            return True
        self._items[key] = current
        self._items.move_to_end(key)
        while len(self._items) > self._size:
            self._items.popitem(last=False)
        return False

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


class EventHub:
    """简单的发布/订阅中心，队列满时丢弃最旧消息而不是阻塞事件处理。"""

    def __init__(self, max_queue: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_queue = max_queue

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        async with self.subscribe() as queue:
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield {"type": "ping"}
