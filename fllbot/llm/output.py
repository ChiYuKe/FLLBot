"""Compatibility delivery entry points; execution belongs to DeliveryService."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..delivery_policy import delivery_allowed

if TYPE_CHECKING:
    from ..onebot.segments import MessageSegment
    from ..plugin.context import Context

__all__ = ["delivery_allowed", "send_chat_reply", "send_media_reply", "store_tool_input"]


async def send_chat_reply(ctx: Context, text: str, *, require_ai: bool = True) -> None:
    return await ctx.bot.deliveries.send_text(ctx, text, require_ai=require_ai)


async def send_media_reply(
    ctx: Context, segments: list[MessageSegment], content: str, *, require_ai: bool = True
) -> bool:
    return await ctx.bot.deliveries.send_media(ctx, segments, content, require_ai=require_ai)


async def store_tool_input(ctx: Context) -> None:
    await ctx.bot.conversations.project_input(ctx)
