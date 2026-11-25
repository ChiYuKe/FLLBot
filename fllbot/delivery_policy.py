"""Shared reply preferences and live delivery access checks."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from .plugin.context import Context

ReplyModeHint = Literal["split", "forward"]


class ChatDispatchPolicy(Protocol):
    def can_choose(self, mode: str, text: str | None) -> bool: ...

    def instructions(self, chunk_size: int) -> str: ...

    def tool_instructions(self, tool_names: set[str], *, text_only: bool = False) -> str: ...

    def deduplicate_delivery(self, capabilities: tuple[str, ...]) -> bool: ...

    def should_forward(self, text: str, part_count: int) -> bool: ...

    def markdown_tables(self, text: str) -> list[tuple[int, int]]: ...

    def requests_forward(self, user_text: str) -> bool: ...


def active_chat_dispatch(ctx: Context) -> ChatDispatchPolicy | None:
    plugin = ctx.bot.plugins.get("chat_dispatch")
    if plugin is not None and ctx.bot.permissions.plugin_enabled(plugin.name, ctx.group_id):
        return plugin.chat_dispatch
    return None


def can_dispatch_reply(ctx: Context, mode: str, dispatch: ChatDispatchPolicy) -> bool:
    return not ctx.is_temp_private and not requests_direct_text(ctx.turn_input) and dispatch.can_choose(mode, ctx.turn_input)


# Explicit text-only requests take precedence over the configured long-reply
# image mode. Keep this deliberately narrow so ordinary mentions of images do
# not change delivery behavior.
_PROMPT = r"(?:提示词|\bprompt\b)"
_COPYABLE = r"(?:(?:可以|可|能|方便)(?:直接)?复制|直接复制)(?:粘贴)?"
_SAME_SENTENCE = r"[^。！？!?\n]{0,24}"
_TEXT = r"(?:纯文本|文本|文字)"
_TEXT_ONLY_REQUEST = re.compile(
    r"(?:不要|别)(?:再)?(?:给我|给)?(?:发送?|用)?(?:把[^，,。！？!?\n]{1,24})?"
    r"(?:转成?|变成|渲染成|做成)?(?:图片|图像|截图|图(?=[\s，,。！？!?]|$))"
    rf"|(?:只要|我要|给我|发我|请发|直接发|输出|发送)(?:一?[份版个段条])?\s*{_TEXT}"
    rf"|{_TEXT}(?:内容)?(?:直接)?(?:给我|发我|发出来|输出出来)"
    rf"|{_TEXT}[，,、 ]*(?:不是|不要|别要|而不是)[，,、 ]*(?:图片|图像|截图|图)"
    rf"|{_COPYABLE}\s*的?\s*{_TEXT}"
    rf"|(?:给我|发我|发给我|给个|发个|来个|来一)(?:一?[份版个段条])?\s*"
    rf"{_COPYABLE}\s*的(?=[\s，,。！？!?]|$)"
    rf"|{_PROMPT}{_SAME_SENTENCE}{_COPYABLE}"
    rf"|{_COPYABLE}{_SAME_SENTENCE}{_PROMPT}"
    rf"|(?:markdown|md)\s*(?:源码|源文本|原文)"
    rf"|表格{_SAME_SENTENCE}{_COPYABLE}|{_COPYABLE}\s*的?\s*表格",
    re.IGNORECASE,
)
_DIRECT_TEXT_REQUEST = re.compile(
    rf"直接(?:发送?|回复|贴出?)(?:给我)?\s*(?:{_COPYABLE}的?\s*)?{_TEXT}"
    r"|(?:只要|给我|给我一份|请发|发我|输出|发送)\s*纯文本"
    rf"|(?:只用|用|以)\s*{_TEXT}(?:形式)?(?:回复|回答|告诉我|发送?|发给我)"
    rf"|只要\s*{_TEXT}|{_TEXT}(?:就行|就好|即可)"
    r"|(?:不要|别)(?:再)?(?:用|发)?(?:合并转发|聊天记录)"
    r"|(?:不要|别)(?:再)?转发成聊天记录"
)


def requests_text_only(text: str | None) -> bool:
    """Whether this request calls for a copyable chat reply."""
    return requests_direct_text(text) or bool(text and _TEXT_ONLY_REQUEST.search(text))


def requests_direct_text(text: str | None) -> bool:
    return bool(text and _DIRECT_TEXT_REQUEST.search(text))


def delivery_allowed(ctx: Context, require_ai: bool = True) -> bool:
    """Recheck mutable access immediately before each protocol operation."""
    if not ctx.data_current:
        return False
    if getattr(ctx.connection, "_closed", False):
        return False
    if int(getattr(ctx.connection, "self_id", 0) or 0) != ctx.self_id:
        return False
    permissions = ctx.bot.permissions
    if ctx.is_group:
        if not permissions.group_enabled(ctx.group_id):
            return False
    elif not permissions.allow_private:
        return False
    if ctx.plugin:
        if not permissions.plugin_enabled(ctx.plugin.name, ctx.group_id):
            return False
        if require_ai and not ctx.bot.chat.scene_enabled(
            "group" if ctx.is_group else "private", ctx.group_id if ctx.is_group else ctx.user_id
        ):
            return False
    if ctx.handler and not permissions.check(ctx.handler.permission, ctx):
        return False
    return True
