"""定时提醒插件测试。"""

from __future__ import annotations

import time

from fllbot.builtin_plugins.reminder import humanize_seconds, parse_duration
from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message


def test_parse_duration() -> None:
    assert parse_duration("30s") == 30
    assert parse_duration("10m") == 600
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration("5分钟") == 300
    assert parse_duration("10") == 600  # 默认按分钟
    assert parse_duration("5s") is None  # 小于 10 秒
    assert parse_duration("100d") is None  # 超过 30 天
    assert parse_duration("abc") is None
    assert parse_duration("") is None


def test_humanize() -> None:
    assert humanize_seconds(30) == "30 秒"
    assert humanize_seconds(600) == "10 分钟"
    assert humanize_seconds(7200) == "2 小时"
    assert humanize_seconds(86400) == "1 天"


async def test_create_reminder(bot) -> None:
    conn = FakeConnection()
    await bot.dispatcher.dispatch(
        parse_event(group_message("/remind 30m 喝水", user_id=30003)), conn  # type: ignore[arg-type]
    )
    rows = await bot.storage.list_reminders("group", 20002)
    assert len(rows) == 1
    assert rows[0]["content"] == "喝水"
    assert rows[0]["creator"] == 30003
    assert "30 分钟" in conn.last_text()


async def test_usage_when_missing_args(bot) -> None:
    conn = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message("/remind")), conn)  # type: ignore[arg-type]
    assert "用法" in conn.last_text()


async def test_bad_duration_rejected(bot) -> None:
    conn = FakeConnection()
    await bot.dispatcher.dispatch(
        parse_event(group_message("/remind 3x 喝水", user_id=30003)), conn  # type: ignore[arg-type]
    )
    assert "时长格式不对" in conn.last_text()
    assert await bot.storage.list_reminders("group", 20002) == []


async def test_tick_sends_and_marks_fired(bot) -> None:
    plugin = bot.plugins.get("reminder")
    assert plugin is not None
    connection = FakeConnection()
    bot.manager.add(connection)
    try:
        await bot.storage.add_reminder(
            scene="group",
            scene_id=20002,
            creator=30003,
            content="开会",
            due_at=time.time() - 1,
        )
        await plugin.loops[0].callback(bot)  # type: ignore[operator]

        assert any("开会" in text for text in connection.texts())
        assert await bot.storage.list_reminders("group", 20002) == []
    finally:
        bot.manager.remove(connection)


async def test_tick_without_connection_keeps_reminder(bot) -> None:
    plugin = bot.plugins.get("reminder")
    assert plugin is not None
    await bot.storage.add_reminder(
        scene="group",
        scene_id=20002,
        creator=30003,
        content="离线提醒",
        due_at=time.time() - 1,
    )
    await plugin.loops[0].callback(bot)  # type: ignore[operator]
    rows = await bot.storage.list_reminders("group", 20002)
    assert len(rows) == 1  # 没有连接时不标记为已发送


async def test_list_and_delete(bot) -> None:
    for text in ("一", "二"):
        await bot.dispatcher.dispatch(
            parse_event(group_message(f"/remind 1h {text}", user_id=30003)),  # type: ignore[arg-type]
            FakeConnection(),
        )

    listing = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message("/remind list")), listing)  # type: ignore[arg-type]
    text = listing.last_text()
    assert "一" in text and "二" in text

    rows = await bot.storage.list_reminders("group", 20002)
    deleting = FakeConnection()
    await bot.dispatcher.dispatch(
        parse_event(group_message(f"/remind del {rows[0]['id']}", user_id=30003)),  # type: ignore[arg-type]
        deleting,
    )
    assert len(await bot.storage.list_reminders("group", 20002)) == 1


async def test_delete_requires_creator_or_admin(bot) -> None:
    await bot.dispatcher.dispatch(
        parse_event(group_message("/remind 1h 私人物品", user_id=30003)),  # type: ignore[arg-type]
        FakeConnection(),
    )
    rows = await bot.storage.list_reminders("group", 20002)
    conn = FakeConnection()
    await bot.dispatcher.dispatch(
        parse_event(group_message(f"/remind del {rows[0]['id']}", user_id=30004)),  # type: ignore[arg-type]
        conn,
    )
    assert "只有创建者" in conn.last_text()
    assert len(await bot.storage.list_reminders("group", 20002)) == 1


async def test_scene_limit(bot) -> None:
    plugin = bot.plugins.get("reminder")
    await plugin.settings.set("max_per_scene", 1)

    for text in ("甲", "乙"):
        await bot.dispatcher.dispatch(
            parse_event(group_message(f"/remind 1h {text}", user_id=30003)),  # type: ignore[arg-type]
            FakeConnection(),
        )
    rows = await bot.storage.list_reminders("group", 20002)
    assert len(rows) == 1
