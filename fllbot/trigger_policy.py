"""Structured trigger decisions; no model calls, delivery or persistence."""
from __future__ import annotations

import hashlib
import json
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from .participation import ParticipationDecision, ParticipationSignals, evaluate


@dataclass(frozen=True)
class TriggerDecision:
    outcome: Literal['accept','ignore','reject']
    reason: str
    trigger_kind: str
    effective_policy_version: str
    text: str = ''
    record_text: str = ''
    fixed_replies: tuple[str, ...] = ()
    settings: dict = field(default_factory=dict, repr=False)
    participation: ParticipationDecision | None = None
    # 这条消息是否明确在叫 Bot（@ / 唤醒词）；沙盒用它区分“明确请求”和“自动参与”。
    addressed: bool = False


def _addressed_elsewhere(ctx: Any) -> bool:
    """消息以 @ 别人开头时，是在跟别人说话，不当作在叫 Bot。"""
    targets: list[str] = []
    for segment in ctx.message:
        # QQ replies prepend a metadata segment before the actual @ recipient.
        # It is not spoken content and must not hide an address to another member.
        if segment.type == "reply":
            continue
        if segment.type == "at":
            targets.append(str(segment.get("qq")))
            continue
        if segment.is_text and not segment.text_content.strip():
            continue
        break
    if not targets:
        return False
    own = str(ctx.self_id)
    return "all" not in targets and own not in targets


class TriggerPolicy:
    def __init__(self, settings: Any, permissions: Any, available: Any,
                 scene_enabled: Any, wake_word: Any) -> None:
        self.settings = settings
        self.permissions = permissions
        self.available = available
        self.scene_enabled = scene_enabled
        self.wake_word = wake_word
        # 只保留参与度判断所需的短期状态，不存消息正文。
        self._bot_replies: dict[tuple[int, int], list[float]] = {}
        self._recent_texts: dict[tuple[int, int], deque[str]] = {}

    def observe_reply(self, ctx: Any) -> None:
        """记录 Bot 刚发出的回复，用于判断“接上一句”和避免连续刷屏。"""
        key = (ctx.self_id, int(ctx.group_id or 0))
        now = time.monotonic()
        replies = [stamp for stamp in self._bot_replies.get(key, []) if now - stamp <= 300]
        replies.append(now)
        self._bot_replies[key] = replies[-12:]

    def _recent_state(self, ctx: Any, text: str) -> tuple[float | None, int, bool]:
        key = (ctx.self_id, int(ctx.group_id or 0))
        now = time.monotonic()
        replies = [stamp for stamp in self._bot_replies.get(key, []) if now - stamp <= 300]
        self._bot_replies[key] = replies
        seconds = now - replies[-1] if replies else None
        recent = sum(1 for stamp in replies if now - stamp <= 120)
        # 群数量很多时也只保留最近的会话状态，避免长期运行无限增长。
        if key not in self._recent_texts and len(self._recent_texts) >= 512:
            for stale in list(self._recent_texts)[: len(self._recent_texts) - 511]:
                self._recent_texts.pop(stale, None)
                self._bot_replies.pop(stale, None)
        texts = self._recent_texts.setdefault(key, deque(maxlen=6))
        normalized = text.strip().casefold()
        repeated = bool(normalized) and normalized in texts
        if normalized:
            texts.append(normalized)
        return seconds, recent, repeated

    def _participation(self, ctx: Any, text: str, triggers: set[str], prefix: str,
                       matched_prefix: bool) -> ParticipationDecision:
        seconds, recent, repeated = self._recent_state(ctx, text)
        reply_id = getattr(ctx, "reply_id", 0) or 0
        # 引用内容稍后才会由模型请求构建器读取；这里只用“最近回过话 + 引用了消息”近似接话。
        replied_to_bot = bool(reply_id) and seconds is not None and seconds <= 300
        media = any(segment.type in {"image", "video", "record", "file", "forward"}
                    for segment in ctx.message)
        settings = self.settings(ctx.group_id, ctx.self_id)
        raw = settings.get("participation_threshold", -10)
        threshold = max(-100, min(100, -10 if raw is None else int(raw)))
        signals = ParticipationSignals(
            text=text,
            # @ 与按名字呼叫本身就是“明确找 Bot”，即使该群只勾选了自动参与。
            addressed=bool(ctx.to_me),
            wake_word=bool(prefix) and text.startswith(prefix),
            mentions_bot=bool(prefix) and prefix in (ctx.text or ""),
            question=False,
            reply_to_bot=replied_to_bot,
            media=media,
            repeated=repeated,
            seconds_since_bot_reply=seconds,
            bot_replies_recent=recent,
        )
        return evaluate(signals, threshold)

    def rejected(self, ctx: Any, reason: str) -> TriggerDecision:
        settings = self.settings(ctx.group_id,ctx.self_id) if ctx.is_group and ctx.group_id else {}
        version = hashlib.sha256(json.dumps([settings,ctx.config.model_dump(),self.permissions.group_config(ctx.group_id) if ctx.is_group else {}],sort_keys=True,default=str).encode()).hexdigest()
        return TriggerDecision('reject',reason,'',version)

    def decide(self, ctx: Any) -> TriggerDecision:
        settings = self.settings(ctx.group_id,ctx.self_id) if ctx.is_group and ctx.group_id else {}
        options = ctx.config.chat
        version = hashlib.sha256(json.dumps([settings,ctx.config.model_dump(),self.permissions.group_config(ctx.group_id) if ctx.is_group else {}],sort_keys=True,default=str).encode()).hexdigest()
        kind, text, record_text, fixed = '', ctx.text or ctx.readable, ctx.text or ctx.readable, ()
        participation: ParticipationDecision | None = None
        addressed = False
        def decision(outcome, reason):
            return TriggerDecision(outcome,reason,kind,version,text,record_text,fixed,settings,
                                   participation,addressed)
        if not ctx.is_group or ctx.group_id is None or ctx.command:
            return decision('ignore','not_agent_message')
        if not ctx.data_current:
            return decision('reject','context_cleared')
        if not self.permissions.group_enabled(ctx.group_id) or not self.permissions.plugin_enabled('agent',ctx.group_id):
            return decision('reject','access_denied')
        if not text or any(prefix and text.startswith(prefix) for prefix in ctx.config.bot.command_prefix):
            return decision('ignore','command_or_empty')
        if not settings.get('auto_group',False):
            return decision('ignore','response_disabled')
        if _addressed_elsewhere(ctx):
            return decision('ignore','addressed_elsewhere')
        triggers = set(settings['trigger_modes'])
        prefix = options.wake_word.strip() or self.wake_word(ctx)
        matched_prefix = 'prefix' in triggers and bool(prefix) and text.startswith(prefix)
        if not ('all' in triggers or 'at' in triggers and ctx.to_me or matched_prefix):
            return decision('ignore','trigger_not_matched')
        explicit = ('at' in triggers and ctx.to_me) or matched_prefix
        # 是否“明确在叫 Bot”独立于触发模式：只勾自动参与时，@ 和唤醒词仍是明确请求。
        addressed = bool(explicit or ctx.to_me or (bool(prefix) and text.startswith(prefix)))
        kind = 'at' if ctx.to_me else ('prefix' if matched_prefix else 'automatic')
        if 'all' in triggers and not explicit:
            probability = max(0,min(100,int(settings.get('auto_reply_probability',100))))
            if random.randrange(100) >= probability:
                return decision('ignore','probability_skipped')
            # 确定性参与判断先于模型调用：纯语气词、复读和刷屏不必消耗一次请求。
            participation = self._participation(ctx, text, triggers, prefix, matched_prefix)
            if not participation.participate:
                return decision('ignore', 'participation_below_threshold')
        only_wake = bool(prefix) and text.rstrip(' ：:,，') == prefix
        if matched_prefix or only_wake:
            remainder = text[len(prefix):].lstrip(' ：:,，')
            if remainder:
                text = record_text = remainder
            else:
                if options.wake_only_mode == 'ignore':
                    return decision('ignore','wake_word_ignored')
                record_text = ctx.text or record_text
                if options.wake_only_mode == 'fixed':
                    fixed = tuple(options.wake_only_replies)
                    if not fixed:
                        return decision('ignore','fixed_reply_missing')
                else:
                    text = options.wake_only_prompt if options.wake_only_mode == 'prompt' else ctx.text or prefix
        if not fixed and (not self.available() or not self.scene_enabled('group',ctx.group_id)):
            return decision('reject','scene_ai_disabled')
        cooldown = float(settings.get('cooldown_seconds',5) or 0)
        key = ('agent',f'natural_language:{ctx.self_id}:{ctx.group_id}',ctx.user_id)
        if not self.permissions.cooldown_ok(key,cooldown):
            return decision('ignore','agent_cooldown')
        return decision('accept','trigger_matched')
