"""Durable text/media delivery and controlled recovery, without model/tool execution."""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from .delivery_policy import delivery_allowed
from .onebot.connection import ActionError, confirmed_message_id
from .onebot.segments import MessageSegment, as_segments

if TYPE_CHECKING:
    from .plugin.context import Context


class DeliveryService:
    def __init__(
        self,
        storage: Any,
        context_factory: Any,
        lock_factory: Any,
        after_sent: Any = None,
        track: Any = None,
        capacity: Any = None,
        serial: Any = None,
        track_request: Any = None,
        replies: Any = None,
        project_input: Any = None,
        allowed: Any = delivery_allowed,
        image_renderer: Any = None,
    ) -> None:
        self.storage = storage
        self.replies = replies
        self.project_input = project_input
        self.allowed = allowed
        self.image_renderer = image_renderer
        self.track_request = track_request or (lambda ctx: nullcontext())
        self.capacity = capacity
        self.serial = serial or (lambda ctx: self.lock_factory(ctx.self_id, ctx.event.scene, ctx.event.scene_id))
        self.context_factory = context_factory
        self.lock_factory = lock_factory
        self.after_sent = after_sent
        self.track = track or (lambda ctx: nullcontext())

    async def recover(self, turn_id: str) -> dict:
        with self.track_request(None):
            initial = await self.storage.recovery_plan(turn_id)
            if initial is None:
                raise ValueError("此轮次没有可以安全恢复的分段")
            ctx = await self.context_factory(initial)
            required_ai = initial["metadata"].get("require_ai", True)
            if ctx is None or not self.allowed(ctx, required_ai):
                raise ValueError("原账号、会话权限或数据代次已变化，不能恢复发送")
            admission = self.capacity.admit(ctx) if self.capacity else nullcontext()
            with admission:
                async with self.serial(ctx):
                    plan = await self.storage.claim_recovery(turn_id)
                    if plan is None:
                        raise ValueError("轮次状态已变化，请重新读取")
                    with self.track(ctx):
                        try:
                            async with asyncio.timeout_at(ctx.received.monotonic_deadline):
                                for index, part in enumerate(plan["parts"]):
                                    if index and plan["metadata"].get("interval_ms", 0):
                                        await asyncio.sleep(plan["metadata"]["interval_ms"] / 1000)
                                    refreshed = await self.context_factory(plan)
                                    if refreshed is None:
                                        ctx.cancellation_reason = "recovery_access_changed"
                                    else:
                                        ctx.event.sender = refreshed.sender
                                    if not self.allowed(ctx, required_ai):
                                        await self.storage.set_turn_status(
                                            turn_id, "cancelled", "recovery_access_changed"
                                        )
                                        break
                                    await self.storage.update_delivery(
                                        plan["delivery_id"], part["part_index"], "sending"
                                    )
                                    if not self.allowed(ctx, required_ai):
                                        await self.storage.update_delivery(
                                            plan["delivery_id"], part["part_index"], "cancelled"
                                        )
                                        await self.storage.set_turn_status(
                                            turn_id, "cancelled", "recovery_access_changed"
                                        )
                                        break
                                    try:
                                        segments = plan["metadata"]["part_segments"][str(part["part_index"])]
                                        receipt = await ctx.send(as_segments(segments))
                                        message_id = confirmed_message_id(receipt)
                                    except BaseException as exc:
                                        rejected = isinstance(exc, ActionError) and exc.retcode is not None
                                        status = "failed" if rejected else "uncertain"
                                        await self.storage.update_delivery(
                                            plan["delivery_id"], part["part_index"], status, error=type(exc).__name__
                                        )
                                        await self.storage.set_turn_status(
                                            turn_id,
                                            "failed" if rejected else "delivery_uncertain",
                                            "recovery_" + status,
                                        )
                                        raise
                                    await self.storage.update_delivery(
                                        plan["delivery_id"],
                                        part["part_index"],
                                        "sent",
                                        message_id=message_id,
                                    )
                                    await self._after_sent(ctx, part["content"])
                                else:
                                    await self.storage.set_turn_status(
                                        turn_id,
                                        "delivered" if ctx.data_current else "cancelled",
                                        "manual_delivery_recovery" if ctx.data_current else "recovery_access_changed",
                                    )
                        except BaseException:
                            # Never restart a model/tool turn; each protocol attempt already has its own fact.
                            states = await self.storage.turn_delivery_state(turn_id)
                            if "sending" in states or "uncertain" in states:
                                await self.storage.set_turn_status(
                                    turn_id, "delivery_uncertain", "recovery_interrupted"
                                )
                            else:
                                await self.storage.set_turn_status(
                                    turn_id,
                                    "cancelled" if ctx.cancellation_reason else "failed",
                                    ctx.cancellation_reason or "recovery_interrupted",
                                )
                            raise
            return await self.storage.turn_detail(turn_id)

    async def _after_sent(self, ctx, text):
        if self.after_sent is not None:
            try:
                await self.after_sent(ctx, text)
            except Exception as exc:
                logging.getLogger('fllbot.delivery').warning('送达后记忆写入失败：%s',type(exc).__name__)

    async def send_media(
        self, ctx: Context, segments: list[MessageSegment], content: str, *, require_ai: bool = True
    ) -> bool:
        if not self.allowed(ctx, require_ai):
            return False
        with self.track_request(ctx):
            return await self._send_media(ctx, segments, content, require_ai=require_ai)

    async def send_text(self, ctx: Context, text: str, *, require_ai: bool = True) -> None:
        if not self.allowed(ctx, require_ai):
            return
        with self.track_request(ctx):
            await self._send_text(ctx, text, require_ai=require_ai)

    async def _send_media(
        self, ctx: Context, segments: list[MessageSegment], content: str, *, require_ai: bool = True
    ) -> bool:
        """Persist a tool's media candidate and receipt; uncertain media is never replayed."""
        if not self.allowed(ctx, require_ai):
            return False
        if self.project_input is not None:
            await self.project_input(ctx)
        segments = getattr(ctx, 'targeted_segments', as_segments)(segments)
        delivery_id = uuid.uuid4().hex
        store = self.storage
        metadata = {
            "mode": "media",
            "format_version": 1,
            "part_segments": {"0": [segment.to_dict() for segment in segments]},
            "scope_epoch": ctx.received.scope_generation,
            "account_epoch": ctx.received.account_epoch,
            "user_id": ctx.user_id,
            "plugin": ctx.plugin.name if ctx.plugin else "",
            "permission": ctx.handler.permission if ctx.handler else None,
            "require_ai": require_ai,
        }
        if not await store.prepare_delivery(
            delivery_id,
            ctx.self_id,
            ctx.event.scene,
            ctx.event.scene_id,
            content,
            [content],
            ctx.data_epoch,
            ctx.turn_id,
            metadata=metadata,
        ):
            return False
        if not self.allowed(ctx, require_ai):
            await store.update_delivery(delivery_id, 0, "cancelled")
            return False
        await store.update_delivery(delivery_id, 0, "sending")
        if not self.allowed(ctx, require_ai):
            await store.update_delivery(delivery_id, 0, "cancelled")
            return False
        try:
            receipt = await ctx.send(segments)
            message_id = confirmed_message_id(receipt)
        except BaseException as exc:
            rejected = isinstance(exc, ActionError) and exc.retcode is not None
            await store.update_delivery(delivery_id, 0, "failed" if rejected else "uncertain", error=type(exc).__name__)
            raise
        await store.update_delivery(
            delivery_id, 0, "sent", message_id=message_id
        )
        await self._after_sent(ctx, content)
        return True

    async def _send_text(self, ctx: Context, text: str, *, require_ai: bool = True) -> None:
        if not self.allowed(ctx, require_ai):
            ctx.trace_event("trigger", "context_cleared", "skipped")
            return

        plan = self.replies.build(
            text,
            turn_id=ctx.turn_id,
            self_id=ctx.self_id,
            scene="group" if ctx.is_group else "private",
            scene_id=ctx.group_id if ctx.is_group else ctx.user_id,
            message_id=ctx.message_id,
            user_id=ctx.user_id,
            options=ctx.reply_settings or ctx.config.chat,
            default_chunk_size=ctx.reply_default_chunk_size or ctx.config.llm.chunk_size,
            nickname=getattr(ctx.connection, "nickname", "") or "FLLBot",
        )
        chunks = [part.text for part in plan.parts]
        if not chunks:
            return
        use_forward = plan.mode == "forward"
        target_segments = getattr(ctx, 'targeted_segments', as_segments)
        delivery_id = uuid.uuid4().hex
        store = self.storage
        parts = plan.persisted_parts()
        if not await store.prepare_delivery(
            delivery_id,
            ctx.self_id,
            "group" if ctx.is_group else "private",
            ctx.group_id if ctx.is_group else ctx.user_id,
            text,
            parts,
            ctx.data_epoch,
            ctx.turn_id,
            metadata={
                "mode": "forward" if use_forward else "split",
                "scope_epoch": ctx.received.scope_generation,
                "account_epoch": ctx.received.account_epoch,
                "user_id": ctx.user_id,
                "plugin": ctx.plugin.name if ctx.plugin else "",
                "permission": ctx.handler.permission if ctx.handler else None,
                "require_ai": require_ai,
                "memory_message_id": ctx.memory_message_id,
                **plan.formatting(),
                'part_segments': {str(part.index): [segment.to_dict() for segment in target_segments(part.segments())]
                                  for part in plan.parts},
            },
        ):
            return
        if use_forward:
            if not self.allowed(ctx, require_ai):
                return
            await store.update_delivery(delivery_id, 0, "sending")
            if not self.allowed(ctx, require_ai):
                await store.update_delivery(delivery_id, 0, "cancelled")
                return
            name = plan.nickname
            nodes = [MessageSegment.node(ctx.self_id, name,
                ([MessageSegment.at(ctx.user_id), MessageSegment.text(' ')] if ctx.is_group and getattr(ctx, 'turn_input', None) is not None else [])
                + [MessageSegment.text(chunk)]).to_dict() for chunk in chunks]
            try:
                with ctx.trace_span("send", "forward"):
                    if ctx.is_group and ctx.group_id is not None:
                        receipt = await ctx.connection.send_group_forward_msg(ctx.group_id, nodes)
                    elif ctx.is_private and ctx.user_id:
                        receipt = await ctx.connection.send_private_forward_msg(ctx.user_id, nodes)
                    else:
                        raise ValueError("无法确定聊天记录的发送目标")
                    message_id = confirmed_message_id(receipt)
            except BaseException as exc:
                rejected = isinstance(exc, ActionError) and exc.retcode is not None
                await store.update_delivery(
                    delivery_id, 0, "failed" if rejected else "uncertain",
                    error=f'ActionError:retcode={exc.retcode}' if rejected else type(exc).__name__
                )
                if not rejected:
                    raise
                # Cancel all text fallback slots before rendering: a long answer
                # must never turn into a burst of ordinary group messages.
                for part in plan.parts:
                    await store.update_delivery(delivery_id, part.index, 'cancelled')
                logging.getLogger('fllbot.delivery').warning(
                    '聊天记录转发被拒绝（retcode=%s），改用图片发送', exc.retcode)
                if not self.allowed(ctx, require_ai):
                    return
                fallback_prefix = [segment for segment in plan.parts[0].segments() if segment.type in {'reply', 'at'}]
                if any(segment.type == 'at' for segment in fallback_prefix):
                    fallback_prefix.append(MessageSegment.text(' '))
                try:
                    if self.image_renderer is None:
                        raise ValueError('image_renderer_unavailable')
                    image = await asyncio.to_thread(self.image_renderer, text)
                    if not isinstance(image, bytes) or not image:
                        raise ValueError('empty_reply_image')
                except Exception as render_error:
                    ctx.turn_reason = 'forward_image_failed'
                    ctx.trace_event('send', 'forward_image_failed', 'failed', type(render_error).__name__)
                    notice = '合并转发失败，长文转图片也未能完成，本次已停止发送，避免刷屏。'
                    await self._send_media(ctx, [*fallback_prefix, MessageSegment.text(notice)], notice, require_ai=require_ai)
                    return
                if not self.allowed(ctx, require_ai):
                    return
                await store.update_delivery(delivery_id, 0, 'superseded')
                await self._send_media(ctx, [*fallback_prefix, MessageSegment.image(
                    'base64://' + base64.b64encode(image).decode('ascii'))], text, require_ai=require_ai)
                return
            else:
                await store.update_delivery(delivery_id,0,'sent',message_id=message_id)
                await self._after_sent(ctx,text)
                return
        for index, chunk in enumerate(chunks):
            if index and plan.interval_ms:
                await asyncio.sleep(plan.interval_ms / 1000)
            part_index = index + int(use_forward)
            if not self.allowed(ctx, require_ai):
                await store.update_delivery(delivery_id, part_index, "cancelled")
                break
            await store.update_delivery(delivery_id, part_index, "sending")
            if not self.allowed(ctx, require_ai):
                await store.update_delivery(delivery_id, part_index, "cancelled")
                break
            try:
                receipt = await ctx.send(plan.parts[index].segments())
                message_id = confirmed_message_id(receipt)
            except BaseException as exc:
                rejected = isinstance(exc, ActionError) and exc.retcode is not None
                await store.update_delivery(
                    delivery_id, part_index, "failed" if rejected else "uncertain", error=type(exc).__name__
                )
                raise
            await store.update_delivery(
                delivery_id,
                part_index,
                "sent",
                message_id=message_id,
            )
            await self._after_sent(ctx, chunk)
