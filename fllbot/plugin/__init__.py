"""插件 API：``from fllbot.plugin import Plugin``。"""

from __future__ import annotations

from ..onebot.segments import MessageSegment
from ..rules import (
    all_rules,
    any_rule,
    command,
    endswith,
    fullmatch,
    in_group,
    in_private,
    keyword,
    not_rule,
    regex,
    startswith,
    to_me,
)
from .agent import ToolContent, ToolEvent, ToolRejection
from .base import AIToolSpec, HandlerKind, HandlerSpec, LoopSpec, Plugin, ServiceCheckSpec, TaskSourceSpec
from .context import Context
from .rendering import load_font
from .settings import MASK, PluginSettings, SettingField, SettingsView


async def send_media_reply(
    ctx: Context, segments: list[MessageSegment], content: str, *, require_ai: bool = True
) -> bool:
    """Send plugin media through the host delivery and permission pipeline."""
    return await ctx.bot.deliveries.send_media(ctx, segments, content, require_ai=require_ai)


__all__ = [
    "MASK",
    "AIToolSpec",
    "Context",
    "MessageSegment",
    "ToolContent",
    "ToolEvent",
    "ToolRejection",
    "HandlerKind",
    "HandlerSpec",
    "LoopSpec",
    "Plugin",
    "PluginSettings",
    "SettingField",
    "ServiceCheckSpec",
    "TaskSourceSpec",
    "SettingsView",
    "all_rules",
    "any_rule",
    "command",
    "endswith",
    "fullmatch",
    "in_group",
    "in_private",
    "keyword",
    "load_font",
    "not_rule",
    "regex",
    "send_media_reply",
    "startswith",
    "to_me",
]
