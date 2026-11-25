# Plugin: emotion | Role: mood engine and whisper scheduling | Metadata: plugin.toml
"""Track per-group mood and send proactive group-temporary private messages.

The engine keeps only recent group chatter in memory; durable mood, whisper
history and opt-outs live in :class:`MoodStore`. All outbound messages pass the
same plugin/group/private-switch checks as normal handler delivery.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fllbot.plugin import Context

from .mood import Mood, judge
from .store import MoodStore
from .whisper import KIND_LABELS, build_prompt, choose_kind, clean_reply, local_line

MAX_ACTIVITY = 120
MAX_GROUPS = 50
MAX_TICK_GROUPS = 50
FLUSH_SECONDS = 10.0
DEFAULT_LLM_TIMEOUT = 45.0
MAX_SESSION_WINDOW_MINUTES = 120
WHISPER_MAX_TOKENS = 350
BASE_VALENCE = 0.12
BASE_ENERGY = 0.55
_LOCAL = timezone(timedelta(hours=8))

# 悄悄话后的私聊闲聊规则：覆盖人格提示词，让模型明白这是熟人短聊，不是智能体任务。
SESSION_CHAT_RULES = (
    "\n\n当前是悄悄话之后的临时私聊：你刚给对方发了一条悄悄话，现在 TA 回来找你单聊。"
    "像熟悉的群友一样直接回话，简短自然，一般两三句以内；不要列条目，不要发链接或二维码。"
    "不要提“20分钟”“私聊窗口”“临时会话”这类规则性说法，也不要提模型、AI 或提示词。"
    "收到图片或表情时结合前文自然接话。只有占位文字时，不要猜测画面内容。"
    "这里没有任何工具可用：不要声称要去查资料、执行操作或稍后回复，做不到就直接说出来。"
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _int_setting(view: Any, key: str, default: int) -> int:
    try:
        return int(view.get(key, default))
    except (TypeError, ValueError):
        return default


def _float_setting(view: Any, key: str, default: float) -> float:
    try:
        return float(view.get(key, default))
    except (TypeError, ValueError):
        return default


def _day_start(now: float) -> float:
    local = datetime.fromtimestamp(now, _LOCAL)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _parse_minutes(value: str) -> int:
    hour_text, minute_text = str(value).strip().split(":", 1)
    hour, minute = int(hour_text), int(minute_text)
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError("时间超出范围")
    return hour * 60 + minute


def _in_quiet_hours(spec: object, now: float) -> bool:
    text = str(spec or "").strip()
    if not text or "-" not in text:
        return False
    try:
        start_text, end_text = text.split("-", 1)
        start = _parse_minutes(start_text)
        end = _parse_minutes(end_text)
    except (TypeError, ValueError):
        return False
    if start == end:
        return False
    local = datetime.fromtimestamp(now, _LOCAL)
    current = local.hour * 60 + local.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _bar(percent: int) -> str:
    filled = max(0, min(10, round(percent / 10)))
    return "█" * filled + "░" * (10 - filled)


def _fit(text: str, limit: int) -> str:
    value = text.strip()
    if len(value) <= limit:
        return value
    cut = value[:limit]
    for mark in "。！？!?…":
        index = cut.rfind(mark)
        if index >= limit // 2:
            return cut[: index + 1]
    return cut.rstrip("，,、；;：: ") + "…"


@dataclass
class Activity:
    user_id: int
    nickname: str
    text: str
    at: float
    message_id: int = 0
    addressed: bool = False


@dataclass
class Candidate:
    user_id: int
    nickname: str
    weight: float = 1.0


class EmotionEngine:
    def __init__(self, plugin: Any, store: MoodStore, *, clock: Any = time.time) -> None:
        self.plugin = plugin
        self.store = store
        self.clock = clock
        self._moods: dict[tuple[int, int], Mood] = store.load_moods()
        self._activity: dict[tuple[int, int], deque[Activity]] = {}
        self._optouts: set[tuple[int, int]] = store.load_optouts()
        # 每个 Bot、用户保留各来源群和投递方式的窗口，避免缺群号时误选。
        self._sessions = store.recent_sent_targets(
            self._now() - MAX_SESSION_WINDOW_MINUTES * 60
        )
        self._dirty: set[tuple[int, int]] = set()
        self._lock = asyncio.Lock()
        self._rng = random.Random()
        self._last_flush = 0.0

    # -- 查询 ------------------------------------------------------------- #
    @property
    def mood_count(self) -> int:
        return len(self._moods)

    def _now(self) -> float:
        return float(self.clock())

    def mood_for(self, self_id: int, group_id: int) -> Mood:
        mood = self._mood(self_id, group_id)
        self._decay(mood, self._now())
        return mood

    def _mood(self, self_id: int, group_id: int) -> Mood:
        key = (int(self_id), int(group_id))
        mood = self._moods.get(key)
        if mood is None:
            mood = Mood(self_id=int(self_id), group_id=int(group_id), updated_at=self._now())
            self._moods[key] = mood
        return mood

    def _decay(self, mood: Mood, now: float) -> None:
        view = self.plugin.settings.view(mood.group_id)
        minutes = _float_setting(view, "mood_decay_minutes", 45.0)
        mood.decay(
            now,
            base_valence=BASE_VALENCE,
            base_energy=BASE_ENERGY,
            half_life_minutes=minutes,
        )
        self._dirty.add((mood.self_id, mood.group_id))

    # -- 观察 ------------------------------------------------------------- #
    def observe(self, ctx: Context) -> None:
        """Update mood, recent activity and whether this user has talked to the bot."""
        if ctx.is_private:
            if ctx.user_id > 0 and ctx.user_id != ctx.self_id:
                self._record_interaction(ctx.self_id, 0, ctx.user_id, self._now())
            return
        if not ctx.is_group or not ctx.group_id or ctx.user_id <= 0:
            return
        key = (int(ctx.self_id), int(ctx.group_id))
        if ctx.user_id == ctx.self_id:
            return
        now = self._now()
        text = (ctx.text or "").strip()
        readable = (ctx.readable or "").strip()
        content = text or readable
        mood = self._mood(*key)
        self._decay(mood, now)
        repeated = bool(text) and self._is_repeated(key, ctx.user_id, text, now)
        delta_v, delta_e, reason = judge(
            content,
            addressed=bool(ctx.to_me),
            hour=datetime.fromtimestamp(now, _LOCAL).hour,
            repeated=repeated,
        )
        mood.valence = _clamp(mood.valence + delta_v, -1.0, 1.0)
        mood.energy = _clamp(mood.energy + delta_e + 0.004, 0.0, 1.0)
        mood.last_reason = reason or "群里在聊天"
        mood.last_event_at = now
        mood.updated_at = now
        self._dirty.add(key)
        if self._addressed_to_bot(ctx, text):
            self._record_interaction(ctx.self_id, ctx.group_id, ctx.user_id, now)

        bucket = self._activity.setdefault(key, deque(maxlen=MAX_ACTIVITY))
        bucket.append(
            Activity(
                user_id=int(ctx.user_id),
                nickname=_short(ctx.nickname, 40),
                text=_short(text, 160),
                at=now,
                message_id=int(ctx.message_id or 0),
                addressed=bool(ctx.to_me),
            )
        )
        if len(self._activity) > MAX_GROUPS:
            self._prune_activity(now)

    def observe_poke(self, ctx: Context) -> None:
        """A poke is a small, unmistakable sign that someone noticed the bot."""
        if not ctx.is_group or not ctx.group_id or ctx.user_id <= 0:
            return
        if ctx.user_id == ctx.self_id:
            return
        now = self._now()
        mood = self._mood(ctx.self_id, ctx.group_id)
        self._decay(mood, now)
        mood.valence = _clamp(mood.valence + 0.05, -1.0, 1.0)
        mood.energy = _clamp(mood.energy + 0.03, 0.0, 1.0)
        mood.last_reason = "被戳了一下"
        mood.last_event_at = now
        mood.updated_at = now
        self._dirty.add((mood.self_id, mood.group_id))
        self._record_interaction(ctx.self_id, ctx.group_id, ctx.user_id, now)

    def _addressed_to_bot(self, ctx: Context, text: str) -> bool:
        if ctx.to_me:
            return True
        if not text:
            return False
        for prefix in getattr(ctx.config.bot, "command_prefix", ()) or ():
            if prefix and text.startswith(prefix):
                return True
        try:
            wake = str(ctx.bot.bots.wake_word(ctx.self_id, getattr(ctx.connection, "nickname", "")) or "")
        except Exception:
            wake = ""
        return bool(wake and text.startswith(wake))

    def _record_interaction(self, self_id: int, group_id: int, user_id: int, at: float) -> None:
        try:
            self.store.record_interaction(self_id, group_id, user_id, at)
        except Exception:
            self.plugin.logger.debug("记录用户互动失败", exc_info=True)

    def praise(self, ctx: Context) -> Mood:
        """Someone deliberately cheered the bot up."""
        now = self._now()
        mood = self._mood(ctx.self_id, ctx.group_id or 0)
        self._decay(mood, now)
        mood.valence = _clamp(mood.valence + 0.12, -1.0, 1.0)
        mood.energy = _clamp(mood.energy + 0.04, 0.0, 1.0)
        mood.last_reason = "被哄了"
        mood.last_event_at = now
        mood.updated_at = now
        self._dirty.add((mood.self_id, mood.group_id))
        return mood

    def reset(self, self_id: int, group_id: int) -> None:
        key = (int(self_id), int(group_id))
        mood = Mood(self_id=int(self_id), group_id=int(group_id), updated_at=self._now())
        mood.last_reason = "被重置了"
        mood.last_event_at = mood.updated_at
        self._moods[key] = mood
        self._dirty.add(key)

    def opt_out(self, self_id: int, user_id: int) -> None:
        key = (int(self_id), int(user_id))
        self._optouts.add(key)
        self._sessions.pop(key, None)
        self.store.set_optout(key[0], key[1], True)

    def opt_in(self, self_id: int, user_id: int) -> None:
        key = (int(self_id), int(user_id))
        self._optouts.discard(key)
        self.store.set_optout(key[0], key[1], False)

    # -- 悄悄话后的私聊互聊 ------------------------------------------------ #
    def _chat_window_seconds(self) -> int:
        view = self.plugin.settings.view(None)
        return max(0, _int_setting(view, "chat_window_minutes", 20)) * 60

    def session_started(self, self_id: int, user_id: int) -> float:
        """互聊窗口的开始时间；不在窗口内（或已过期）返回 ``0.0``。"""
        sessions = self._active_sessions(self_id, user_id)
        return max(sessions.values(), default=0.0)

    def _active_sessions(self, self_id: int, user_id: int) -> dict[tuple[int, str], float]:
        key = (int(self_id), int(user_id))
        window = self._chat_window_seconds()
        if key in self._optouts or window <= 0:
            return {}
        cutoff = self._now() - window
        return {source: started for source, started in self._sessions.get(key, {}).items() if started >= cutoff}

    def session_group_id(self, self_id: int, user_id: int) -> int | None:
        """群号缺失时，只用当前窗口内唯一可确认的临时会话来源。"""
        sessions = self._active_sessions(self_id, user_id)
        possible_groups = {group_id for group_id, delivery in sessions if delivery != "private"}
        if len(possible_groups) == 1:
            group_id = next(iter(possible_groups))
            if group_id > 0 and (group_id, "temp") in sessions:
                return group_id
        return None

    def _prune_sessions(self, now: float) -> None:
        cutoff = now - MAX_SESSION_WINDOW_MINUTES * 60
        for key, sessions in list(self._sessions.items()):
            remaining = {source: started for source, started in sessions.items() if started >= cutoff}
            if remaining:
                self._sessions[key] = remaining
            else:
                del self._sessions[key]

    async def session_reply(
        self,
        *,
        bot: Any,
        ctx: Context,
        text: str,
        started_at: float,
    ) -> str:
        """在互聊窗口内沿用对话历史快聊。"""
        ctx.turn_input = text
        async with bot.conversations.turn(ctx) as accepted:
            if not accepted:
                return ""
            persona = str(self.plugin.settings.view(None).get("persona") or "").strip()
            try:
                messages = await bot.chat.build_messages(
                    "private",
                    ctx.user_id,
                    text,
                    ctx=ctx,
                    persona_name=persona or None,
                    include_history=True,
                    # 只带这次悄悄话之后的历史，避免把很久以前的私聊旧账翻出来。
                    history_since=started_at,
                    system_rules=SESSION_CHAT_RULES,
                )
                reply = await asyncio.wait_for(
                    bot.chat.complete(
                        messages,
                        scene="private",
                        scene_id=ctx.user_id,
                        task="chat",
                        self_id=ctx.self_id,
                    ),
                    timeout=DEFAULT_LLM_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 与智能体路径一致：模型失败静默收尾，不让 dispatcher 发通用错误提示。
                ctx.turn_reason = "model_failed"
                self.plugin.logger.debug("私聊互聊生成失败：%s", type(exc).__name__)
                return ""
            reply_text = str(getattr(reply, "text", "") or "").strip()
            if not reply_text or not ctx.data_current:
                return ""
            await bot.conversations.project_input(ctx, ctx.context_input or text)
            await bot.deliveries.send_text(ctx, reply_text, require_ai=False)
            return reply_text

    # -- 周期任务 --------------------------------------------------------- #
    async def tick(
        self,
        bot: Any,
        *,
        now: float | None = None,
        force: bool = False,
        rng: random.Random | None = None,
    ) -> dict[str, Any] | None:
        """Try at most one whisper; return what was sent, or ``None``."""
        if not getattr(bot, "started", False) or getattr(bot, "stopping", False):
            return None
        moment = float(now if now is not None else self._now())
        picker = rng or self._rng
        async with self._lock:
            self._decay_all(moment)
            self._maybe_flush(moment)
            self._prune_activity(moment)
            self._prune_sessions(moment)
            for key in self._groups():
                self_id, group_id = key
                view = self.plugin.settings.view(group_id)
                if self._block_reason(bot, group_id, view):
                    continue
                if not self._within_limits(key, view, moment):
                    continue
                if not force:
                    chance = _int_setting(view, "probability", 35)
                    if chance <= 0 or picker.random() * 100.0 >= chance:
                        continue
                member = self._pick_member(key, view, moment, picker)
                if member is None:
                    continue
                return await self._deliver(bot, key, member, view, moment, picker)
            return None

    async def whisper_to(
        self,
        *,
        bot: Any,
        self_id: int,
        group_id: int,
        user_id: int,
        nickname: str = "",
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Send on an explicit request without waiting for the ambient schedule."""
        now = self._now()
        async with self._lock:
            view = self.plugin.settings.view(group_id)
            reason = self._block_reason(bot, group_id, view)
            if reason:
                return {"error": reason, "message": "当前不允许主动私聊。"}
            if int(self_id) <= 0 or int(user_id) <= 0:
                return {"error": "invalid_target", "message": "缺少有效的用户。"}
            if (int(self_id), int(user_id)) in self._optouts:
                return {"error": "opted_out", "message": "对方关闭了悄悄话。"}
            # 显式请求也受每日额度约束：否则群友可以反复让机器人发临时会话，
            # 绕过 tick 的每群/全局上限，把主动私聊变成刷屏入口。
            day_start = _day_start(now)
            daily = _int_setting(view, "daily_limit", 3)
            if daily > 0 and self.store.count_since(int(self_id), day_start, group_id=int(group_id)) >= daily:
                return {"error": "daily_limit", "message": "本群今天的悄悄话额度已经用完。"}
            global_limit = _int_setting(view, "global_daily_limit", 8)
            if global_limit > 0 and self.store.count_since(int(self_id), day_start) >= global_limit:
                return {"error": "global_daily_limit", "message": "今天的悄悄话总量已经用完。"}
            connection = bot.manager.by_self_id(int(self_id))
            if connection is None:
                return {"error": "bot_offline", "message": "机器人当前不在线。"}
            key = (int(self_id), int(group_id))
            member = Candidate(user_id=int(user_id), nickname=_short(nickname, 40) or f"QQ {user_id}")
            return await self._deliver(
                bot,
                key,
                member,
                view,
                now,
                self._rng,
                kind=kind,
                explicit=True,
            )

    def _block_reason(self, bot: Any, group_id: int, view: Any) -> str:
        if not bool(view.get("enabled", True)):
            return "disabled"
        if not bool(view.get("whisper_enabled", True)):
            return "whisper_disabled"
        if not bot.permissions.plugin_enabled(self.plugin.name, group_id):
            return "plugin_disabled"
        if not bot.permissions.group_enabled(group_id):
            return "group_disabled"
        if not bot.permissions.allow_private:
            return "private_disabled"
        return ""

    def _within_limits(self, key: tuple[int, int], view: Any, now: float) -> bool:
        self_id, group_id = key
        if _in_quiet_hours(view.get("quiet_hours", ""), now):
            return False
        day_start = _day_start(now)
        daily = _int_setting(view, "daily_limit", 3)
        if daily > 0 and self.store.count_since(self_id, day_start, group_id=group_id) >= daily:
            return False
        global_limit = _int_setting(view, "global_daily_limit", 8)
        if global_limit > 0 and self.store.count_since(self_id, day_start) >= global_limit:
            return False
        interval = max(0, _int_setting(view, "min_interval_minutes", 45)) * 60
        last = self.store.last_group_at(self_id, group_id)
        if interval and last and now - last < interval:
            return False
        return True

    def _groups(self) -> list[tuple[int, int]]:
        items: list[tuple[float, tuple[int, int]]] = []
        for key, bucket in self._activity.items():
            if bucket:
                items.append((bucket[-1].at, key))
        items.sort(key=lambda item: item[0], reverse=True)
        return [key for _, key in items[:MAX_TICK_GROUPS]]

    def _pick_member(
        self,
        key: tuple[int, int],
        view: Any,
        now: float,
        rng: random.Random,
    ) -> Candidate | None:
        self_id, _group_id = key
        window = max(60, _int_setting(view, "active_window_minutes", 20) * 60)
        cutoff = now - window
        entries = [
            item
            for item in self._activity.get(key, ())
            if cutoff <= item.at <= now and item.user_id != self_id and item.user_id > 0
        ]
        minimum = max(1, _int_setting(view, "min_messages", 3))
        if len(entries) < minimum:
            return None
        if bool(view.get("only_addressed", False)):
            entries = [item for item in entries if item.addressed]
        if not entries:
            return None

        grouped: dict[int, Candidate] = {}
        for item in entries:
            if not item.text:
                continue
            candidate = grouped.get(item.user_id)
            if candidate is None:
                candidate = Candidate(user_id=item.user_id, nickname=item.nickname, weight=0.0)
                grouped[item.user_id] = candidate
            candidate.weight += 1.0 + max(0.0, 1.0 - (now - item.at) / window)
            if not candidate.nickname and item.nickname:
                candidate.nickname = item.nickname
        if not grouped:
            return None

        cooldown = max(0, _int_setting(view, "per_user_cooldown_hours", 24)) * 3600
        pool: list[Candidate] = []
        for candidate in grouped.values():
            if (self_id, candidate.user_id) in self._optouts:
                continue
            last = self.store.last_user_at(self_id, candidate.user_id)
            if cooldown and last and now - last < cooldown:
                continue
            pool.append(candidate)
        if not pool:
            return None

        if bool(view.get("only_interacted", True)):
            days = max(1, _int_setting(view, "interaction_window_days", 30))
            interacted = self.store.interacted_users(self_id, key[1], now - days * 86400)
            pool = [candidate for candidate in pool if candidate.user_id in interacted]
            if not pool:
                return None

        last_target = self.store.last_group_target(self_id, key[1])
        if last_target:
            others = [candidate for candidate in pool if candidate.user_id != last_target]
            if others:
                pool = others
        return rng.choices(pool, weights=[max(0.1, item.weight) for item in pool], k=1)[0]

    async def _deliver(
        self,
        bot: Any,
        key: tuple[int, int],
        member: Candidate,
        view: Any,
        now: float,
        rng: random.Random,
        *,
        kind: str | None = None,
        explicit: bool = False,
    ) -> dict[str, Any]:
        self_id, group_id = key
        connection = bot.manager.by_self_id(self_id)
        if connection is None:
            return {"sent": False, "error": "bot_offline"}
        chosen = kind if kind in KIND_LABELS else choose_kind(str(view.get("style", "mix")), self._mood(*key), rng)
        max_chars = max(16, min(200, _int_setting(view, "max_chars", 150)))
        recent = self._recent_lines(key)
        text = ""
        if bool(view.get("llm_enabled", True)) and bool(getattr(bot.chat, "available", False)):
            text = await self._model_line(bot, key, member, chosen, view, recent, max_chars)
        if not text:
            avoid = set(self.store.recent_group_contents(self_id, group_id, 6))
            text = _fit(local_line(self._mood(*key), chosen, member.nickname, rng, avoid=avoid), max_chars)
        if not text:
            return {"sent": False, "error": "empty_text"}

        delivery = str(view.get("delivery", "temp"))
        try:
            if delivery == "private":
                await connection.send_private_msg(member.user_id, text)
            else:
                await connection.send_private_msg(member.user_id, text, group_id=group_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                self.store.record_whisper(
                    self_id=self_id,
                    group_id=group_id,
                    user_id=member.user_id,
                    nickname=member.nickname,
                    kind=chosen,
                    content=text,
                    created_at=now,
                    status="failed",
                    error=type(exc).__name__,
                )
            except Exception:
                self.plugin.logger.debug("记录失败悄悄话时出错", exc_info=True)
            self.plugin.logger.info(
                "悄悄话发送失败：群 %s 用户 %s（%s）", group_id, member.user_id, type(exc).__name__
            )
            return {
                "sent": False,
                "error": "send_failed",
                "group_id": group_id,
                "user_id": member.user_id,
                "kind": chosen,
            }

        sent_at = self._now()
        try:
            self.store.record_whisper(
                self_id=self_id,
                group_id=group_id,
                user_id=member.user_id,
                nickname=member.nickname,
                kind=chosen,
                content=text,
                created_at=sent_at,
                status="sent",
                delivery=delivery,
            )
        except Exception:
            self.plugin.logger.debug("记录悄悄话发送结果失败", exc_info=True)
        mood = self._mood(self_id, group_id)
        mood.valence = _clamp(mood.valence + 0.02, -1.0, 1.0)
        mood.energy = _clamp(mood.energy - 0.01, 0.0, 1.0)
        mood.last_reason = "刚说完悄悄话"
        mood.updated_at = now
        self._dirty.add(key)
        if bool(view.get("archive_history", True)):
            try:
                await bot.storage.add_chat_message(
                    "private",
                    member.user_id,
                    "assistant",
                    text,
                    self_id=self_id,
                    user_id=member.user_id,
                    nickname=member.nickname,
                )
            except Exception:
                self.plugin.logger.debug("悄悄话写入私聊历史失败", exc_info=True)
        # 悄悄话送达即打开私聊互聊窗口：这段时间内对方的私聊由 session_reply 快速接话。
        window_minutes = max(0, _int_setting(view, "chat_window_minutes", 20))
        if window_minutes > 0:
            sessions = self._sessions.setdefault((int(self_id), int(member.user_id)), {})
            sessions[(int(group_id), delivery)] = sent_at
        else:
            self._sessions.pop((int(self_id), int(member.user_id)), None)
        return {
            "sent": True,
            "explicit": bool(explicit),
            "group_id": group_id,
            "user_id": member.user_id,
            "nickname": member.nickname,
            "kind": chosen,
            "delivery": delivery,
            "text": text,
            "mood": mood.label,
        }

    async def _model_line(
        self,
        bot: Any,
        key: tuple[int, int],
        member: Candidate,
        kind: str,
        view: Any,
        recent: list[str],
        max_chars: int,
    ) -> str:
        self_id, group_id = key
        bot_name = str(bot.bots.nickname(self_id) or "我")
        persona = str(view.get("persona") or "").strip()
        if not persona:
            persona = str(bot.chat.persona_name("group", group_id, self_id) or "")
        try:
            system = bot.chat.system_prompt("group", group_id, persona_name=persona or None)
        except Exception:
            self.plugin.logger.debug("读取人格失败，悄悄话改用本地句子", exc_info=True)
            return ""
        system = f"你在当前群里的昵称是「{bot_name}」。\n{system}"
        prompt = build_prompt(
            bot_name=bot_name,
            nickname=member.nickname or f"QQ {member.user_id}",
            mood=self._mood(*key),
            kind=kind,
            recent=recent,
            max_chars=max_chars,
            include_context=bool(view.get("include_context", True)),
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        try:
            reply = await asyncio.wait_for(
                bot.chat.complete(
                    messages,
                    max_tokens=WHISPER_MAX_TOKENS,
                    scene="group",
                    scene_id=group_id,
                    task="chat",
                    self_id=self_id,
                ),
                timeout=DEFAULT_LLM_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.plugin.logger.debug("生成悄悄话失败：%s", type(exc).__name__)
            return ""
        return clean_reply(getattr(reply, "text", ""), max_chars)

    def _recent_lines(self, key: tuple[int, int]) -> list[str]:
        bucket = self._activity.get(key, ())
        lines: list[str] = []
        for item in reversed(bucket):
            if not item.text:
                continue
            name = item.nickname or f"QQ {item.user_id}"
            lines.append(f"{name}：{item.text[:80]}")
            if len(lines) >= 6:
                break
        lines.reverse()
        return lines

    # -- 展示 ------------------------------------------------------------- #
    def whisper_status(self, self_id: int, group_id: int, user_id: int) -> dict[str, Any]:
        now = self._now()
        view = self.plugin.settings.view(group_id)
        daily = _int_setting(view, "daily_limit", 3)
        global_limit = _int_setting(view, "global_daily_limit", 8)
        interval = max(0, _int_setting(view, "min_interval_minutes", 45)) * 60
        last = self.store.last_group_at(self_id, group_id)
        next_in = 0
        if interval and last and last <= now:
            next_in = max(0, int((last + interval - now + 59) // 60))
        return {
            "enabled": bool(view.get("enabled", True)),
            "whisper_enabled": bool(view.get("whisper_enabled", True)),
            "private_allowed": bool(self.plugin.bot.permissions.allow_private),
            "opted_out": (int(self_id), int(user_id)) in self._optouts,
            "sent_today": self.store.count_since(self_id, _day_start(now), group_id=group_id),
            "sent_today_all": self.store.count_since(self_id, _day_start(now)),
            "daily_limit": daily,
            "global_daily_limit": global_limit,
            "only_interacted": bool(view.get("only_interacted", True)),
            "interaction_window_days": max(1, _int_setting(view, "interaction_window_days", 30)),
            "next_in_minutes": next_in,
            "quiet_hours": str(view.get("quiet_hours", "") or ""),
            "quiet_now": _in_quiet_hours(view.get("quiet_hours", ""), now),
            "delivery": str(view.get("delivery", "temp") or "temp"),
        }

    def status_text(self, ctx: Context) -> str:
        if ctx.group_id is None:
            return "情绪是按群记录的，去群里发 /情绪 看吧。"
        mood = self.mood_for(ctx.self_id, ctx.group_id)
        data = self.whisper_status(ctx.self_id, ctx.group_id, ctx.user_id)
        lines = [
            f"现在的心情：{mood.label} {mood.emoji}",
            f"心情 {_bar(mood.valence_percent)}  精神 {_bar(mood.energy_percent)}",
            f"最近：{mood.last_reason or '还没发生什么'}",
        ]
        if not data["enabled"]:
            lines.append("本群的情绪插件已关闭。")
        elif not data["whisper_enabled"]:
            lines.append("悄悄话已关闭，只记录情绪。")
        elif data["opted_out"]:
            lines.append("你已关闭悄悄话；发 /悄悄话 开启 可以恢复。")
        elif not data["private_allowed"]:
            lines.append("全局私聊开关已关闭，不会发悄悄话。")
        elif data["quiet_now"]:
            lines.append("现在是安静时段，暂时不会发悄悄话。")
        else:
            limit = data["daily_limit"]
            total = f"本群今天已发 {data['sent_today']}/{limit} 条" if limit > 0 else f"本群今天已发 {data['sent_today']} 条"
            if limit > 0 and data["sent_today"] >= limit:
                total += "，额度用完了。"
            elif data["next_in_minutes"] > 0:
                total += f"，下次大约 {data['next_in_minutes']} 分钟后。"
            else:
                total += "，随时可能来一条。"
            lines.append(total)
            if data["only_interacted"]:
                lines.append(f"只挑 {data['interaction_window_days']} 天内和我说过话的群友。")
        lines.append("发 /悄悄话 关闭 可以不再收到私聊。")
        return "\n".join(lines).strip()

    def whisper_text(self, ctx: Context) -> str:
        data = self.whisper_status(ctx.self_id, ctx.group_id or 0, ctx.user_id)
        if data["opted_out"]:
            return "你目前关闭了悄悄话；发 /悄悄话 开启 可以恢复。"
        if not data["private_allowed"]:
            return "全局私聊开关已关闭，所以不会发悄悄话。"
        if not data["enabled"]:
            return "本群的情绪插件已关闭。"
        if not data["whisper_enabled"]:
            return "本群的悄悄话已关闭，只记录情绪。"
        lines = [
            "悄悄话会以群临时会话发到你的私聊，只在你最近和我说过话、又在群里聊天时出现。",
            "内容可能是牢骚，也可能是一句悄悄话；不想收随时可以关。",
            "发 /悄悄话 关闭 关闭；发 /悄悄话 开启 恢复。",
        ]
        limit = data["daily_limit"]
        if limit > 0:
            lines.append(f"本群今天已发 {data['sent_today']}/{limit} 条。")
        return "\n".join(lines)

    def mood_payload(self, ctx: Context) -> dict[str, Any]:
        if not ctx.is_group or ctx.group_id is None:
            return {"error": "group_only", "message": "情绪只在群聊里记录。"}
        mood = self.mood_for(ctx.self_id, ctx.group_id)
        return {**mood.summary(), "whisper": self.whisper_status(ctx.self_id, ctx.group_id, ctx.user_id)}

    # -- 持久化与清理 ----------------------------------------------------- #
    def _decay_all(self, now: float) -> None:
        for mood in list(self._moods.values()):
            self._decay(mood, now)

    def _prune_activity(self, now: float) -> None:
        cutoff = now - 6 * 3600
        stale = [key for key, bucket in self._activity.items() if not bucket or bucket[-1].at < cutoff]
        for key in stale:
            self._activity.pop(key, None)

    def _is_repeated(self, key: tuple[int, int], user_id: int, text: str, now: float) -> bool:
        for item in reversed(self._activity.get(key, ())):
            if now - item.at > 180:
                break
            if item.user_id == user_id and item.text == text:
                return True
        return False

    def _maybe_flush(self, now: float) -> None:
        if now < self._last_flush or now - self._last_flush < FLUSH_SECONDS:
            return
        self.flush()

    def flush(self) -> None:
        if not self._dirty:
            return
        moods = [self._moods[key] for key in list(self._dirty) if key in self._moods]
        self._dirty.clear()
        try:
            self.store.save_moods(moods)
            self._last_flush = self._now()
        except Exception:
            self._dirty.update((mood.self_id, mood.group_id) for mood in moods)
            self.plugin.logger.debug("保存情绪状态失败", exc_info=True)

    async def close(self) -> None:
        self.flush()
        self.store.close()


def _short(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


__all__ = ["EmotionEngine"]
