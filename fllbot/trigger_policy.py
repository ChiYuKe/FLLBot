"""Structured trigger decisions; no model calls, delivery or persistence."""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any, Literal


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


def _addressed_elsewhere(ctx: Any) -> bool:
    """消息以 @ 别人开头时，是在跟别人说话，不当作在叫 Bot。"""
    targets: list[str] = []
    for segment in ctx.message:
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

    def rejected(self, ctx: Any, reason: str) -> TriggerDecision:
        settings = self.settings(ctx.group_id,ctx.self_id) if ctx.is_group and ctx.group_id else {}
        version = hashlib.sha256(json.dumps([settings,ctx.config.model_dump(),self.permissions.group_config(ctx.group_id) if ctx.is_group else {}],sort_keys=True,default=str).encode()).hexdigest()
        return TriggerDecision('reject',reason,'',version)

    def decide(self, ctx: Any) -> TriggerDecision:
        settings = self.settings(ctx.group_id,ctx.self_id) if ctx.is_group and ctx.group_id else {}
        options = ctx.config.chat
        version = hashlib.sha256(json.dumps([settings,ctx.config.model_dump(),self.permissions.group_config(ctx.group_id) if ctx.is_group else {}],sort_keys=True,default=str).encode()).hexdigest()
        kind, text, record_text, fixed = '', ctx.text or ctx.readable, ctx.text or ctx.readable, ()
        def decision(outcome, reason):
            return TriggerDecision(outcome,reason,kind,version,text,record_text,fixed,settings)
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
        kind = 'at' if 'at' in triggers and ctx.to_me else ('prefix' if matched_prefix else 'automatic')
        if 'all' in triggers and not explicit:
            probability = max(0,min(100,int(settings.get('auto_reply_probability',100))))
            if random.randrange(100) >= probability:
                return decision('ignore','probability_skipped')
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
