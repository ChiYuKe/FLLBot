"""emotion 插件：情绪状态、临时会话悄悄话、命令与工具。"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from fllbot.llm.client import LLMClient
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
    llm: dict[str, Any] = {"enabled": model, "api_key": "fake-key" if model else ""}
    if model:
        # 真实走模型路径的用例再配合 patch_client 使用；只 mock complete 的用例不受影响。
        llm.update(
            {
                "base_url": "http://mock.local",
                "model": "test-model",
                "cooldown_seconds": 0,
                "max_context_messages": 6,
                "context_mode": "custom",
                "max_context_chars": 2000,
                "chunk_size": 500,
            }
        )
    bot = await make_bot(
        tmp_path,
        plugin_dirs=[plugin_root],
        llm=llm,
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


def make_transport(reply_text: str, captured: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply_text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    return httpx.MockTransport(handler)


def patch_client(bot, transport: httpx.MockTransport) -> None:
    http = httpx.AsyncClient(transport=transport)
    bot.chat.client = LLMClient(lambda: bot.config.llm, http_client=http)


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
        payload = json.loads(output)
        assert payload["error"] == "addressed_required"
        assert "silent" not in payload and "reply" not in payload
        assert connection.calls == []
    finally:
        await bot.stop()


async def test_send_whisper_tool_is_silent_only_after_delivery(tmp_path, monkeypatch):
    bot, emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        entry = bot.tools.snapshot().get("emotion__send_whisper")
        assert entry is not None
        registry = {entry.name: (entry.owner, entry.spec)}
        ctx = Context(
            bot, connection, parse_event(group_message("悄悄跟我说一句", at=SELF)), handler=entry.spec
        )
        ctx.trigger_decision = SimpleNamespace(trigger_kind="at", addressed=True)
        output, silent = await bot.tool_executor.execute(
            ctx, "emotion__send_whisper", {"kind": "secret"}, registry, refresh=lambda _: ([], registry)
        )
        payload = json.loads(output)
        assert payload.get("sent") is True, payload
        assert payload.get("silent") is True, payload
        assert 0 < len(payload.get("reply") or "") <= 60, payload
        assert silent is True
        assert any(call[0] == "send_private_msg" for call in connection.calls)

        # 发送失败（或结果不确定）时不能直出确认，交给模型如实说明。
        monkeypatch.setattr(
            engine, "whisper_to", AsyncMock(return_value={"sent": False, "error": "send_failed"})
        )
        callback = next(tool.callback for tool in emotion.ai_tools if tool.name == "send_whisper")
        failed_ctx = Context(
            bot,
            connection,
            parse_event(group_message("悄悄跟我说一句", user_id=OTHER, message_id=2, at=SELF)),
            plugin=emotion,
        )
        failed_ctx.trigger_decision = SimpleNamespace(trigger_kind="at", addressed=True)
        failed = await callback(failed_ctx, kind="secret")
        assert failed["sent"] is False and failed["error"] == "send_failed"
        assert "silent" not in failed and "reply" not in failed
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


async def test_whisper_can_be_longer_and_invites_reply(tmp_path, monkeypatch):
    """悄悄话按 max_chars 写两到四句，并留一句让对方想回的话头。"""
    bot, _emotion, engine, connection = await setup_emotion(
        tmp_path, monkeypatch, model=True, max_chars=150
    )
    try:
        long_text = (
            "偷偷跟你说呀，今天群里聊得挺热闹的，我在旁边看得挺开心。"
            "你最近忙不忙呀？有空回我一句呗，我在呢。"
        )
        captured: list[dict[str, Any]] = []
        patch_client(bot, make_transport(long_text, captured))
        await deliver(bot, connection, "今天好热闹", message_id=1)
        result = await engine.tick(bot, force=True)

        assert result is not None and result["sent"] is True
        assert result["text"] == long_text
        assert captured[0]["max_tokens"] == 350
        prompt = captured[0]["messages"][-1]["content"]
        assert "两到四句" in prompt
        assert "最多 150 个汉字" in prompt
        assert "想回你" in prompt
    finally:
        await bot.stop()


@pytest.mark.parametrize(("sub_type", "source_group", "reply_group"), [
    ("friend", None, None),
    ("group", GROUP + 1, GROUP + 1),
    ("group", None, GROUP),
])
async def test_whisper_opens_private_chat_window(tmp_path, monkeypatch, sub_type, source_group, reply_group):
    """悄悄话送达后打开互聊窗口：窗口内的私聊由快聊接话，不经过智能体。"""
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch, model=True)
    try:
        whisper_text = "今天也在偷偷看你们聊天呀。最近过得怎么样？"
        captured: list[dict[str, Any]] = []
        patch_client(bot, make_transport(whisper_text, captured))
        await deliver(bot, connection, "今天好累啊", message_id=1)
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"] is True
        target = result["user_id"]
        assert engine.session_started(SELF, target) > 0

        # 窗口内私聊：只发生第二次模型调用（快聊），不再有工具/智能体参与。
        payload = private_message("在吗？", user_id=target)
        payload.update(sub_type=sub_type, group_id=source_group)
        await bot.on_raw_event(connection, payload)
        await bot.dispatcher.drain()
        assert len(captured) == 2
        session_messages = captured[1]["messages"]
        assert any("临时私聊" in item["content"] for item in session_messages if item["role"] == "system")
        # 上下文带上了这条悄悄话和本次互聊历史，不带更早的旧账。
        assert any(whisper_text in item["content"] for item in session_messages)
        assert session_messages[-1] == {"role": "user", "content": "在吗？"}

        private_replies = [call[1] for call in connection.calls
                           if call[0] == "send_private_msg" and call[1].get("user_id") == target]
        assert len(private_replies) == 2
        assert text_of(private_replies[-1]) == whisper_text
        assert private_replies[-1].get("group_id") == reply_group
        rows = await bot.storage.recent_chat("private", target, self_id=SELF)
        assert [(row.role, row.content) for row in rows] == [
            ("assistant", whisper_text),
            ("user", "在吗？"),
            ("assistant", whisper_text),
        ]
    finally:
        await bot.stop()


@pytest.mark.parametrize(("segment_type", "caption", "model_text"), [
    ("image", "", "[图片]"),
    ("mface", "", "[表情]"),
    ("face", "", "[表情]"),
    ("image", "你看", "你看[图片]"),
])
async def test_temp_whisper_media_and_followup_use_same_llm_chat(
    tmp_path, monkeypatch, segment_type, caption, model_text
):
    bot, _, engine, connection = await setup_emotion(tmp_path, monkeypatch, model=True)
    try:
        captured: list[dict[str, Any]] = []
        model_reply = "你刚才那句让我想接着聊。"
        patch_client(bot, make_transport(model_reply, captured))
        await deliver(bot, connection, "聊一会儿")
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"]
        target = result["user_id"]
        payload = private_message(caption, user_id=target)
        payload["sub_type"] = "group"
        payload["message"].append({"type": segment_type, "data": {"id": "1"}})
        followup = private_message("你接着说", user_id=target, message_id=3)
        followup["sub_type"] = "group"

        await bot.on_raw_event(connection, payload)
        await bot.on_raw_event(connection, followup)
        await bot.dispatcher.drain()

        assert len(captured) == 3
        media_turn, followup_turn = (call["messages"] for call in captured[1:])
        assert media_turn[-1] == {"role": "user", "content": model_text}
        assert {"role": "user", "content": model_text} in followup_turn[:-1]
        assert followup_turn[-2:] == [
            {"role": "assistant", "content": model_reply},
            {"role": "user", "content": "你接着说"},
        ]
        replies = [params for action, params in connection.calls if action == "send_private_msg"]
        assert len(replies) == 3
        assert [params["group_id"] for params in replies] == [GROUP, GROUP, GROUP]
        assert text_of(replies[-1]) == model_reply
    finally:
        await bot.stop()


async def test_whisper_with_multiple_possible_groups_skips_unroutable_reply(tmp_path, monkeypatch):
    bot, _, engine, connection = await setup_emotion(tmp_path, monkeypatch, model=True)
    try:
        engine.clock = time.time
        captured: list[dict[str, Any]] = []
        patch_client(bot, make_transport("悄悄说一句，今天还行。", captured))
        await deliver(bot, connection, "聊一会儿")
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"]
        target = result["user_id"]
        engine.store.record_whisper(
            self_id=SELF, group_id=GROUP + 1, user_id=target, nickname="群友", kind="secret",
            content="另一群的悄悄话", created_at=time.time(), status="sent", delivery="temp",
        )
        await bot.plugins.reload("emotion")
        engine = bot.plugins.get("emotion").engine
        assert engine.session_started(SELF, target)
        assert engine.session_group_id(SELF, target) is None
        payload = private_message("在吗？", user_id=target)
        payload["sub_type"] = "group"

        await bot.on_raw_event(connection, payload)
        await bot.dispatcher.drain()

        assert len(captured) == 1
        assert len([call for call in connection.calls if call[0] == "send_private_msg"]) == 1
    finally:
        await bot.stop()


async def test_chat_window_expires_after_timeout(tmp_path, monkeypatch):
    """超过窗口时长后不再接私聊，也不发模型请求。"""
    bot, _emotion, engine, connection = await setup_emotion(
        tmp_path, monkeypatch, model=True, chat_window_minutes=20
    )
    try:
        captured: list[dict[str, Any]] = []
        patch_client(bot, make_transport("悄悄说一句，今天还行。你呢？", captured))
        await deliver(bot, connection, "聊聊吧", message_id=1)
        send_private = connection.send_private_msg

        async def send_with_delay(*args, **kwargs):
            engine.clock = lambda: BASE + 30
            return await send_private(*args, **kwargs)

        monkeypatch.setattr(connection, "send_private_msg", send_with_delay)
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"] is True
        assert engine.session_started(SELF, USER) == BASE + 30

        engine.clock = lambda: BASE + 20 * 60 + 1
        assert engine.session_started(SELF, USER) == BASE + 30
        engine.clock = lambda: BASE + 30 + 20 * 60 + 1
        assert engine.session_started(SELF, USER) == 0.0
        await bot.dispatcher.dispatch(
            parse_event(private_message("还在吗", user_id=USER, message_id=2)), connection
        )
        assert len(captured) == 1  # 只有发悄悄话那次模型调用
        replies = [call for call in connection.calls if call[0] == "send_private_msg"]
        assert len(replies) == 1
    finally:
        await bot.stop()


async def test_chat_window_can_be_disabled_and_opt_out_closes_it(tmp_path, monkeypatch):
    bot, _emotion, engine, connection = await setup_emotion(
        tmp_path, monkeypatch, chat_window_minutes=0
    )
    try:
        await deliver(bot, connection, "不想被私聊", message_id=1)
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"] is True
        assert engine.session_started(SELF, USER) == 0.0

        # 单独验证 opt_out 会关掉已打开的窗口
        await _emotion.settings.update({"chat_window_minutes": 20})
        engine.clock = lambda: BASE + 3601
        await deliver(bot, connection, "在聊呢", message_id=2)
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"] is True
        assert engine.session_started(SELF, USER) > 0
        engine.opt_out(SELF, USER)
        assert engine.session_started(SELF, USER) == 0.0
    finally:
        await bot.stop()


async def test_chat_window_survives_reload(tmp_path, monkeypatch):
    """窗口从悄悄话发送记录恢复：重启（重载）后短时间内私聊仍能接上。"""
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch)
    try:
        engine.clock = time.time  # 恢复逻辑按真实时间差判断，改用真实时钟
        await deliver(bot, connection, "聊一会儿", message_id=1)
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"] is True
        assert engine.session_started(SELF, USER) > 0

        await bot.plugins.reload("emotion")
        reloaded = bot.plugins.get("emotion")
        assert reloaded is not None and reloaded.engine is not None
        assert reloaded.engine.session_started(SELF, USER) > 0
        assert reloaded.engine.session_group_id(SELF, USER) == GROUP
    finally:
        await bot.stop()


async def test_session_chat_skips_commands_and_media(tmp_path, monkeypatch):
    """窗口内的命令消息不进快聊：命令走原处理器，快聊不消耗模型请求。"""
    bot, _emotion, engine, connection = await setup_emotion(tmp_path, monkeypatch, model=True)
    try:
        captured: list[dict[str, Any]] = []
        patch_client(bot, make_transport("悄悄说一句，今天还行。", captured))
        await deliver(bot, connection, "聊几句", message_id=1)
        result = await engine.tick(bot, force=True)
        assert result is not None and result["sent"] is True
        assert engine.session_started(SELF, USER) > 0
        texts_before = len(connection.texts())

        await bot.dispatcher.dispatch(
            parse_event(private_message("/悄悄话", user_id=USER, message_id=2)), connection
        )
        assert len(captured) == 1  # 命令不触发快聊
        assert len(connection.texts()) > texts_before  # /悄悄话 命令照常回复
        assert any("悄悄话" in text for text in connection.texts()[texts_before:])
    finally:
        await bot.stop()
