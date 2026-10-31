"""Shared delivery for model replies, including merged forward messages."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..onebot.segments import MessageSegment

if TYPE_CHECKING:
    from ..plugin.context import Context


async def send_chat_reply(ctx: Context, text: str) -> None:
    from .chat import split_text

    options = ctx.config.chat
    chunks = split_text(text, options.reply_chunk_size or ctx.config.llm.chunk_size)
    if not chunks:
        return
    use_forward = options.reply_mode == "forward" or (
        options.reply_mode == "auto" and len(text) >= options.forward_threshold
    )
    if use_forward:
        name = getattr(ctx.connection, "nickname", "") or "FLLBot"
        nodes = [MessageSegment.node(ctx.self_id, name, chunk).to_dict() for chunk in chunks]
        try:
            if ctx.is_group and ctx.group_id is not None:
                await ctx.connection.send_group_forward_msg(ctx.group_id, nodes)
            elif ctx.is_private and ctx.user_id:
                await ctx.connection.send_private_forward_msg(ctx.user_id, nodes)
            else:
                raise ValueError("无法确定聊天记录的发送目标")
            return
        except Exception:
            # Forward support differs between protocol implementations. Preserve the reply.
            ctx.logger.warning("聊天记录转发失败，改为分段发送", exc_info=True)
    for index, chunk in enumerate(chunks):
        if index and options.reply_interval_ms:
            await asyncio.sleep(options.reply_interval_ms / 1000)
        await ctx.reply(chunk, quote=options.quote_reply and index == 0,
                        at_sender=options.at_sender and index == 0)
