"""SQLite 持久化测试。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fllbot.storage import Storage


@pytest.fixture
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "data" / "test.db")
    await instance.connect()
    try:
        yield instance
    finally:
        await instance.close()


async def test_migration_creates_schema(storage: Storage) -> None:
    assert await storage.schema_version() == 2
    summary = await storage.summary()
    assert summary["schema_version"] == 2
    assert summary["messages"] == 0


async def test_kv_roundtrip(storage: Storage) -> None:
    assert await storage.kv_get("demo", "missing", default=1) == 1
    await storage.kv_set("demo", "name", "FLLBot")
    await storage.kv_set("demo", "data", {"a": 1, "b": [1, 2]})
    assert await storage.kv_get("demo", "name") == "FLLBot"
    assert await storage.kv_get("demo", "data") == {"a": 1, "b": [1, 2]}
    assert await storage.kv_all("demo") == {"data": {"a": 1, "b": [1, 2]}, "name": "FLLBot"}
    assert await storage.kv_scopes() == ["demo"]
    await storage.kv_delete("demo", "name")
    assert await storage.kv_get("demo", "name") is None


async def test_group_state(storage: Storage) -> None:
    await storage.upsert_group(20002, enabled=True, config={"llm": {"persona": "concise"}})
    groups = await storage.list_group_states()
    assert groups[20002]["enabled"] is True
    assert groups[20002]["config"]["llm"]["persona"] == "concise"

    await storage.upsert_group(20002, enabled=False)
    groups = await storage.list_group_states()
    assert groups[20002]["enabled"] is False
    assert groups[20002]["config"]["llm"]["persona"] == "concise"


async def test_plugin_states(storage: Storage) -> None:
    await storage.set_plugin_state("ping", 0, False)
    await storage.set_plugin_state("ping", 20002, True)
    states = await storage.list_plugin_states()
    assert states[("ping", 0)] is False
    assert states[("ping", 20002)] is True
    await storage.clear_plugin_states("ping")
    assert await storage.list_plugin_states() == {}


async def test_chat_history(storage: Storage) -> None:
    await storage.add_chat_message("group", 20002, "user", "你好")
    await storage.add_chat_message("group", 20002, "assistant", "你好呀")
    rows = await storage.recent_chat("group", 20002, limit=10)
    assert rows == [("user", "你好"), ("assistant", "你好呀")]
    rooms = await storage.chat_rooms()
    assert rooms[0]["scene_id"] == 20002
    assert rooms[0]["messages"] == 2
    assert await storage.clear_chat("group", 20002) == 2
    assert await storage.recent_chat("group", 20002) == []


async def test_message_stats(storage: Storage) -> None:
    for index in range(3):
        await storage.record_message(
            self_id=10001, scene="group", scene_id=20002, user_id=30003 + index, content=f"m{index}"
        )
    assert await storage.total_messages() == 3
    daily = await storage.daily_message_counts(7)
    assert sum(item["count"] for item in daily) == 3
    top = await storage.top_users(5)
    assert sum(item["count"] for item in top) == 3


async def test_meta(storage: Storage) -> None:
    assert await storage.meta_get("k", default="x") == "x"
    await storage.meta_set("k", {"a": 1})
    assert await storage.meta_get("k") == {"a": 1}


async def test_reconnect_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    first = Storage(path)
    await first.connect()
    await first.kv_set("demo", "k", "v")
    await first.close()

    second = Storage(path)
    await second.connect()
    assert await second.kv_get("demo", "k") == "v"
    await second.close()


async def test_reminders(storage: Storage) -> None:
    due_id = await storage.add_reminder(
        scene="group", scene_id=20002, creator=30003, content="到点了", due_at=time.time() - 5
    )
    future_id = await storage.add_reminder(
        scene="group", scene_id=20002, creator=30003, content="以后", due_at=time.time() + 3600
    )
    assert await storage.count_reminders("group", 20002) == 2

    due = await storage.due_reminders()
    assert [item["id"] for item in due] == [due_id]
    assert due[0]["content"] == "到点了"

    await storage.mark_reminder_fired(due_id)
    assert await storage.due_reminders() == []
    assert len(await storage.list_reminders("group", 20002)) == 1

    assert await storage.delete_reminder(future_id, scene="group", scene_id=20002) is True
    assert await storage.delete_reminder(future_id, scene="group", scene_id=20002) is False
    assert await storage.list_reminders("group", 20002) == []


async def test_reminder_scoped_to_scene(storage: Storage) -> None:
    await storage.add_reminder(
        scene="group", scene_id=20002, creator=1, content="群里的", due_at=time.time() + 60
    )
    await storage.add_reminder(
        scene="private", scene_id=30003, creator=1, content="私聊的", due_at=time.time() + 60
    )
    group_rows = await storage.list_reminders("group", 20002)
    private_rows = await storage.list_reminders("private", 30003)
    assert [item["content"] for item in group_rows] == ["群里的"]
    assert [item["content"] for item in private_rows] == ["私聊的"]
