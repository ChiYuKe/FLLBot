"""emotion 插件：情绪状态、临时会话悄悄话、命令与工具。"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot, poke_notice, private_message

ROOT = Path(__file__).resolve().parents[1]
GROUP = 20002
SELF = 10001
USER = 30003
OTHER = 30004
BASE = datetime(2026, 1, 1, 12, 0, 0).timestamp()


async def setup_emotion(tmp_path, monkeypatch, *, model: bool = False, **settings):
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    shutil.copytree(ROOT / "plugins" / "emotion", plugin_root / "emotion")
    bot = await make_bot(
        tmp_path,
        plugin_dirs=[plugin_root],
        llm={"enabled": model, "api_key": "fake-key" if model else ""},
    )
    await bot.permissions.save_group(GROUP, enabled=True, config={})
    emotion = bot.plugins.get("emotion")
    assert emotion is not None
    values = {
        "enabled": True,
        "whisper_enabled": True,
        "style": "mix",
        "probability": 100,
        "min_interval_minutes": 1,
        "active_window_minutes": 60,
        "min_messages": 1,
        "per_user_cooldown_hours": 1,
        "daily_limit": 5,
        "global_daily_limit": 10,
        "quiet_hours": "",
        "max_chars": 60,
        "llm_enabled": model,
        "include_context": False,
        "archive_history": True,
        "delivery": "temp",
        "persona": "",
        "mood_decay_minutes": 45,
        "only_interacted": False,
        "interaction_window_days": 7,
    }
    values.update(settings)
    await emotion.settings.update(values)
    engine = emotion.engine
    assert engine is not None
    engine.clock = lambda: BASE
    connection = FakeConnection()
    bot.manager.add(connection)
    return bot, emotion, engine, connection


async def deliver(bot, connection, text: str, *, user_id: int = USER, message_id: int = 1, at: int | None = None):
    event = parse_event(group_message(text, user_id=user_id, message_id=message_id, at=at))
    await bot.dispatcher.dispatch(event, connection)


def text_of(call: dict) -> str:
    return "".join(segment.data.get("text", "") for segment in call["message"] if segment.type == "text")


async def test_mood_moves_with_praise_and_insult(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        before = engine.mood_for(SELF, GROUP).valence
        await deliver(bot, connection, "你好可爱，谢谢你")
        after_praise = engine.mood_for(SELF, GROUP).valence
        assert after_praise > before

        await deliver(bot, connection, "笨死了，闭嘴", user_id=OTHER, message_id=2)
        after_insult = engine.mood_for(SELF, GROUP).valence
        assert after_insult < after_praise
        assert engine.mood_for(SELF, GROUP).last_reason
    finally:
        await bot.stop()


async def test_whisper_uses_group_temporary_session(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        await deliver(bot, connection, "今天好累啊", user_id=USER, message_id=1)
        await deliver(bot, connection, "晚上吃什么", user_id=OTHER, message_id=2)
        result = await engine.tick(bot, force=True)

        assert result is not None and result["sent"] is True
        assert result["group_id"] == GROUP
        assert result["user_id"] in {USER, OTHER}
        assert result["kind"] in {"whine", "secret"}
        call = connection.calls[-1]
        assert call[0] == "send_private_msg"
        assert call[1]["group_id"] == GROUP
        assert call[1]["user_id"] == result["user_id"]
        assert text_of(call[1]) == result["text"]

        rows = await bot.storage.recent_chat("private", result["user_id"], self_id=SELF)
        assert rows[-1].role == "assistant"
        assert rows[-1].content == result["text"]
    finally:
        await bot.stop()


async def test_whisper_interval_and_optout(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        await deliver(bot, connection, "就我一个人在说话", message_id=1)
        first = await engine.tick(bot, force=True)
        assert first is not None and first["sent"] is True

        blocked = await engine.tick(bot, now=BASE + 30, force=True)
        assert blocked is None

        engine.opt_out(SELF, USER)
        no_target = await engine.tick(bot, now=BASE + 61, force=True)
        assert no_target is None

        engine.opt_in(SELF, USER)
        await deliver(bot, connection, "我又来了", message_id=2)
        again = await engine.tick(bot, now=BASE + 61, force=True)
        assert again is None  # 同一用户仍在 1 小时冷却中
    finally:
        await bot.stop()


async def test_whisper_respects_quiet_hours(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(
        tmp_path, monkeypatch, quiet_hours="00:00-23:59"
    )
    try:
        await deliver(bot, connection, "这个点还在聊天", message_id=1)
        assert await engine.tick(bot, force=True) is None
        assert connection.calls == []
    finally:
        await bot.stop()


async def test_whisper_uses_model_when_available(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch, model=True)
    try:
        complete = AsyncMock(return_value=SimpleNamespace(text="偷偷说一句，今天还不错。"))
        monkeypatch.setattr(bot.chat, "complete", complete)
        await deliver(bot, connection, "今天还行", message_id=1)
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"] is True
        assert result["text"] == "偷偷说一句，今天还不错。"
        complete.assert_awaited_once()
    finally:
        await bot.stop()


async def test_mood_command_and_praise_command(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        await deliver(bot, connection, "/情绪", message_id=1)
        assert any("心情" in text for text in connection.texts())

        before = engine.mood_for(SELF, GROUP).valence
        await deliver(bot, connection, "/哄哄", message_id=2)
        assert engine.mood_for(SELF, GROUP).valence > before
        assert connection.last_text()
    finally:
        await bot.stop()


async def test_read_mood_tool_returns_state(tmp_path, monkeypatch):
    bot, emotion, _engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        assert any(
            item.name == "emotion_style" and item.tools == ("read_mood",)
            for item in emotion.agent_instructions
        )
        ctx = Context(bot, connection, parse_event(group_message("看看你现在什么心情", at=SELF)))
        entry = bot.tools.snapshot().get("emotion__read_mood")
        assert entry is not None
        registry = {entry.name: (entry.owner, entry.spec)}
        output, silent = await bot.tool_executor.execute(
            ctx, "emotion__read_mood", {}, registry, refresh=lambda _: ([], registry)
        )
        data = json.loads(output)
        assert data["mood"]
        assert "whisper" in data
        assert silent is False
    finally:
        await bot.stop()


async def test_send_whisper_tool_requires_addressed_turn(tmp_path, monkeypatch):
    bot, _emotion, _engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        ctx = Context(bot, connection, parse_event(group_message("他们聊得真热闹")))
        ctx.trigger_decision = SimpleNamespace(addressed=False)
        entry = bot.tools.snapshot().get("emotion__send_whisper")
        assert entry is not None
        registry = {entry.name: (entry.owner, entry.spec)}
        output, _ = await bot.tool_executor.execute(
            ctx, "emotion__send_whisper", {"kind": "secret"}, registry, refresh=lambda _: ([], registry)
        )
        assert json.loads(output)["error"] == "addressed_required"
        assert connection.calls == []
    finally:
        await bot.stop()


async def test_poke_improves_mood(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        before = engine.mood_for(SELF, GROUP).valence
        await bot.dispatcher.dispatch(parse_event(poke_notice()), connection)
        assert engine.mood_for(SELF, GROUP).valence > before
    finally:
        await bot.stop()


async def test_group_plugin_switch_blocks_observation_and_whisper(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        await bot.permissions.set_plugin_enabled("emotion", False, GROUP)
        await deliver(bot, connection, "群里有人吗", message_id=1)
        assert engine.mood_count == 0
        assert await engine.tick(bot, force=True) is None
        assert connection.calls == []
    finally:
        await bot.stop()


async def test_group_setting_can_disable_observation(tmp_path, monkeypatch):
    bot, emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        await emotion.settings.update({"enabled": False}, group_id=GROUP)
        await deliver(bot, connection, "这个群关掉情绪", message_id=1)
        assert engine.mood_count == 0
        assert await engine.tick(bot, force=True) is None
        assert connection.calls == []
    finally:
        await bot.stop()


@pytest.mark.parametrize("value", ["", "25:00-26:00", "abc"])
async def test_invalid_quiet_hours_do_not_block(tmp_path, monkeypatch, value):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch, quiet_hours=value)
    try:
        await deliver(bot, connection, "随便聊聊", message_id=1)
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"] is True
    finally:
        await bot.stop()


async def test_mood_persists_across_reload(tmp_path, monkeypatch):
    bot, emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        await deliver(bot, connection, "你真好，谢谢你", message_id=1)
        value = engine.mood_for(SELF, GROUP).valence
        engine.flush()
        await bot.plugins.reload("emotion")
        reloaded = bot.plugins.get("emotion")
        assert reloaded is not None and reloaded is not emotion
        new_engine = reloaded.engine
        assert new_engine is not None
        new_engine.clock = lambda: BASE
        assert new_engine.mood_for(SELF, GROUP).valence == pytest.approx(value, abs=0.01)
    finally:
        await bot.stop()


async def test_private_delivery_omits_group_id(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch, delivery="private")
    try:
        await deliver(bot, connection, "聊一会儿", message_id=1)
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"] is True
        call = connection.calls[-1]
        assert call[0] == "send_private_msg"
        assert "group_id" not in call[1]
    finally:
        await bot.stop()


async def test_whisper_skips_users_who_never_talked_to_bot(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch, only_interacted=True)
    try:
        await deliver(bot, connection, "今天聊什么", user_id=USER, message_id=1)
        await deliver(bot, connection, "不知道", user_id=OTHER, message_id=2)
        assert await engine.tick(bot, force=True) is None
        assert connection.calls == []
    finally:
        await bot.stop()


async def test_whisper_only_targets_users_who_talked_to_bot(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch, only_interacted=True)
    try:
        await deliver(bot, connection, "你们在聊什么", user_id=USER, message_id=1)
        await deliver(bot, connection, "随便说说", user_id=OTHER, message_id=2)
        await deliver(bot, connection, "机器人你好", user_id=USER, message_id=3, at=SELF)
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"] is True
        assert result["user_id"] == USER
    finally:
        await bot.stop()


async def test_private_chat_counts_as_interaction(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch, only_interacted=True)
    try:
        await bot.dispatcher.dispatch(parse_event(private_message("你好", user_id=USER)), connection)
        await deliver(bot, connection, "群里也在聊", user_id=USER, message_id=2)
        await deliver(bot, connection, "我也说一句", user_id=OTHER, message_id=3)
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"] is True
        assert result["user_id"] == USER
    finally:
        await bot.stop()


async def test_explicit_whisper_shares_daily_quota(tmp_path, monkeypatch):
    """显式发悄悄话也消耗同一个每日额度，不能被反复请求刷出更多临时会话。"""
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch, daily_limit=1)
    try:
        await deliver(bot, connection, "今天好累啊", user_id=USER, message_id=1)
        assert await engine.tick(bot, force=True) is not None

        asked = await engine.whisper_to(
            bot=bot, self_id=SELF, group_id=GROUP, user_id=USER, nickname="小明"
        )
        assert asked.get("sent") is not True and asked["error"] == "daily_limit"
        sent = [call for call in connection.calls if call[0] == "send_private_msg"]
        assert len(sent) == 1
    finally:
        await bot.stop()
