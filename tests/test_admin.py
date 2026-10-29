"""超管热操作插件测试。"""

from __future__ import annotations

from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message


async def dispatch(bot, text: str, *, user_id: int = 99999, role: str = "member") -> FakeConnection:
    conn = FakeConnection()
    await bot.dispatcher.dispatch(
        parse_event(group_message(text, user_id=user_id, role=role)), conn  # type: ignore[arg-type]
    )
    return conn


async def test_status_superuser(bot) -> None:
    conn = await dispatch(bot, "/status")
    text = conn.last_text()
    assert "FLLBot" in text
    assert "插件" in text
    assert "连接" in text


async def test_status_requires_superuser(bot) -> None:
    conn = await dispatch(bot, "/status", user_id=30003)
    assert conn.sent == []


async def test_reload_unknown_plugin(bot) -> None:
    conn = await dispatch(bot, "/reload nope")
    assert "不存在" in conn.last_text()


async def test_reload_known_plugin(bot) -> None:
    conn = await dispatch(bot, "/reload ping")
    assert "已重载" in conn.last_text()
    assert bot.plugins.get("ping") is not None


async def test_plugin_toggle_off(bot) -> None:
    conn = await dispatch(bot, "/plugin off ping")
    assert "已停用" in conn.last_text()
    assert bot.permissions.plugin_enabled("ping", None) is False

    assert (await dispatch(bot, "/ping")).sent == []

    await dispatch(bot, "/plugin on ping")
    assert "pong" in (await dispatch(bot, "/ping")).last_text()


async def test_group_toggle(bot) -> None:
    conn = await dispatch(bot, "/group off")
    assert "已停用" in conn.last_text()
    assert bot.permissions.group_enabled(20002) is False
    assert (await dispatch(bot, "/ping")).sent == []

    await bot.permissions.set_group_enabled(20002, True)
    assert (await dispatch(bot, "/ping")).sent


async def test_group_toggle_requires_admin(bot) -> None:
    conn = await dispatch(bot, "/group off", user_id=30003, role="member")
    assert conn.sent == []
    assert bot.permissions.group_enabled(20002) is True
