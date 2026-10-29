"""群治理插件测试：欢迎、违禁词、刷屏。"""

from __future__ import annotations

import time

import fllbot.builtin_plugins.group_guard as guard
from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message


def notice(notice_type: str, *, user_id: int = 30003, group_id: int = 20002) -> dict:
    return {
        "time": int(time.time()),
        "self_id": 10001,
        "post_type": "notice",
        "notice_type": notice_type,
        "sub_type": "approve",
        "group_id": group_id,
        "user_id": user_id,
        "operator_id": 10001,
    }


async def test_welcome_on_join(bot) -> None:
    conn = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(notice("group_increase")), conn)  # type: ignore[arg-type]
    assert conn.sent
    assert "欢迎" in conn.last_text()
    assert "30003" in conn.last_text()


async def test_welcome_template_group_override(bot) -> None:
    plugin = bot.plugins.get("group_guard")
    await plugin.settings.set("welcome_template", "欢迎新朋友 {nickname}", group_id=20002)

    conn = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(notice("group_increase", user_id=30009)), conn)  # type: ignore[arg-type]
    assert "欢迎新朋友 30009" in conn.last_text()

    # 其他群仍用默认模板
    other = FakeConnection()
    await bot.dispatcher.dispatch(
        parse_event(notice("group_increase", user_id=30010, group_id=20003)), other  # type: ignore[arg-type]
    )
    assert "加入本群" in other.last_text()


async def test_welcome_disabled(bot) -> None:
    plugin = bot.plugins.get("group_guard")
    await plugin.settings.set("welcome_enabled", False, group_id=20002)

    conn = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(notice("group_increase")), conn)  # type: ignore[arg-type]
    assert conn.sent == []


async def test_badword_deleted(bot) -> None:
    guard._recent.clear()
    plugin = bot.plugins.get("group_guard")
    await plugin.settings.set("badwords", ["广告"], group_id=20002)

    conn = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message("这里有一条广告")), conn)  # type: ignore[arg-type]
    assert "delete_msg" in conn.actions()
    assert "违禁词" in conn.last_text()


async def test_badword_mute_and_regex(bot) -> None:
    guard._recent.clear()
    plugin = bot.plugins.get("group_guard")
    await plugin.settings.set("badwords", ["re:加\\s*群\\s*私\\s*聊"], group_id=20002)
    await plugin.settings.set("badword_action", "delete_and_mute", group_id=20002)

    conn = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message("加 群 私 聊 有福利")), conn)  # type: ignore[arg-type]
    assert "set_group_ban" in conn.actions()
    assert "违禁词" in conn.last_text()


async def test_commands_are_not_moderated(bot) -> None:
    guard._recent.clear()
    plugin = bot.plugins.get("group_guard")
    await plugin.settings.set("badwords", ["广告"], group_id=20002)

    conn = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message("/echo 广告")), conn)  # type: ignore[arg-type]
    assert "delete_msg" not in conn.actions()


async def test_spam_triggers_mute(bot) -> None:
    guard._recent.clear()
    plugin = bot.plugins.get("group_guard")
    await plugin.settings.set("spam_enabled", True, group_id=20002)
    await plugin.settings.set("spam_window", 10, group_id=20002)
    await plugin.settings.set("spam_limit", 3, group_id=20002)
    await plugin.settings.set("spam_action", "delete_and_mute", group_id=20002)

    conn = FakeConnection()
    for index in range(3):
        await bot.dispatcher.dispatch(
            parse_event(group_message(f"刷屏测试 {index}", message_id=100 + index, user_id=31000)),  # type: ignore[arg-type]
            conn,
        )
    assert "set_group_ban" in conn.actions()
    assert "刷屏" in conn.last_text()


async def test_welcome_command_updates_settings(bot) -> None:
    conn = FakeConnection()
    await bot.dispatcher.dispatch(
        parse_event(group_message("/welcome set 欢迎 {nickname} 加入", user_id=99999)),  # type: ignore[arg-type]
        conn,
    )
    assert "已更新" in conn.last_text()

    plugin = bot.plugins.get("group_guard")
    assert plugin.settings.get("welcome_template", group_id=20002) == "欢迎 {nickname} 加入"


async def test_badword_command_management(bot) -> None:
    conn = FakeConnection()
    await bot.dispatcher.dispatch(
        parse_event(group_message("/badword add 测试词", user_id=99999)), conn  # type: ignore[arg-type]
    )
    plugin = bot.plugins.get("group_guard")
    assert "测试词" in plugin.settings.get("badwords", group_id=20002)

    await bot.dispatcher.dispatch(
        parse_event(group_message("/badword del 测试词", user_id=99999)), conn  # type: ignore[arg-type]
    )
    assert "测试词" not in (plugin.settings.get("badwords", group_id=20002) or [])


async def test_guard_command_requires_group_admin(bot) -> None:
    conn = FakeConnection()
    await bot.dispatcher.dispatch(
        parse_event(group_message("/guard", user_id=30003, role="member")), conn  # type: ignore[arg-type]
    )
    assert conn.sent == []

    admin = FakeConnection()
    await bot.dispatcher.dispatch(
        parse_event(group_message("/guard", user_id=30003, role="admin")), admin  # type: ignore[arg-type]
    )
    assert "群治理状态" in admin.last_text()
