"""事件去重测试。"""

from __future__ import annotations

import asyncio

from fllbot.events import DedupCache
from helpers import FakeConnection, group_message


def test_cache_ttl_and_eviction() -> None:
    cache = DedupCache(size=2, ttl=60.0)
    assert cache.seen("a", now=0.0) is False
    assert cache.seen("a", now=10.0) is True
    assert cache.seen("b", now=11.0) is False
    assert cache.seen("c", now=12.0) is False  # 淘汰最早的 a
    assert cache.seen("a", now=13.0) is False
    assert len(cache) <= 2
    assert cache.seen("c", now=1000.0) is False  # 过期


async def test_duplicate_message_ignored(bot) -> None:
    connection = FakeConnection()
    payload = group_message("/echo hi", message_id=777)

    await bot.on_raw_event(connection, payload)  # type: ignore[arg-type]
    await bot.on_raw_event(connection, payload)  # type: ignore[arg-type]
    await asyncio.sleep(0.1)

    assert len(connection.sent) == 1


async def test_same_id_different_account_not_deduped(bot) -> None:
    first = FakeConnection(self_id=10001)
    second = FakeConnection(self_id=10002)

    await bot.on_raw_event(first, group_message("/echo hi", self_id=10001, message_id=888))  # type: ignore[arg-type]
    await bot.on_raw_event(second, group_message("/echo hi", self_id=10002, message_id=888))  # type: ignore[arg-type]
    await asyncio.sleep(0.1)

    assert len(first.sent) == 1
    assert len(second.sent) == 1
