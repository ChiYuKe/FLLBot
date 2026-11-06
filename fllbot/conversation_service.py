"""Shared durable turn lifecycle for model and agent entry points."""
from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager, contextmanager, nullcontext
from typing import Any

from .conversation import CapacityExceeded, ServiceClosing, turn_scope
from .tracing import current_trace_id


class ConversationService:
    def __init__(self, storage: Any, lock_factory: Any, config: Any, after_turn: Any = None, execution_scope: Any = None, capacity: Any = None, version: Any = None) -> None:
        self.storage = storage
        self.capacity = capacity
        self.lock_factory = lock_factory
        self.config = config
        self.version = version or (lambda config:hashlib.sha256(config.model_dump_json().encode()).hexdigest())
        self.after_turn = after_turn
        self.execution_scope = execution_scope or (lambda ctx, config: nullcontext())
        self.closing = False
        self._tasks: dict[asyncio.Task, Any] = {}
        self._active: dict[str, tuple[Any, asyncio.Task]] = {}

    async def project_input(self, ctx: Any, text: str | None = None) -> None:
        """Project received input once before the first visible answer or tool reply."""
        while ctx.execution_parent is not None:
            ctx = ctx.execution_parent
        content = text if text is not None else ctx.context_input or ctx.turn_input
        if content is None or ctx.input_projected:
            return
        await self.storage.add_chat_message(ctx.event.scene,ctx.event.scene_id,'user',content,
            user_id=ctx.user_id,nickname=ctx.nickname,sender_role=ctx.sender.role,self_id=ctx.self_id,
            generation=ctx.data_epoch,account_epoch=ctx.received.account_epoch,scope_epoch=ctx.received.scope_generation)
        ctx.input_projected = True

    @contextmanager
    def track_task(self, ctx: Any = None):
        if self.closing:
            raise ServiceClosing('service_stopping')
        task = asyncio.current_task()
        nested = task in self._tasks
        previous = self._tasks.get(task)
        self._tasks[task] = ctx
        try:
            yield
        finally:
            if nested:
                self._tasks[task] = previous
            else:
                self._tasks.pop(task,None)

    def open(self) -> None:
        if self._tasks:
            raise RuntimeError('Cannot reopen conversations with active tasks')
        self.closing = False

    async def close(self) -> None:
        self.closing = True
        tasks = []
        for task,ctx in list(self._tasks.items()):
            if task is asyncio.current_task() or task.done():
                continue
            if ctx is not None:
                ctx.cancellation_reason = 'service_stopping'
            task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)

    @contextmanager
    def track_delivery(self, ctx: Any):
        with self.track_task(ctx):
            self._active[ctx.turn_id] = (ctx, asyncio.current_task())
            try:
                yield
            finally:
                self._active.pop(ctx.turn_id, None)

    def cancel_account(self, self_id: int) -> int:
        turns = [turn_id for turn_id, (ctx, _) in self._active.items() if ctx.self_id == self_id]
        return sum(self.cancel(turn_id, 'account_disabled') for turn_id in turns)

    def can_cancel(self, turn_id: str) -> bool:
        active = self._active.get(turn_id)
        return active is not None and not active[1].done()

    def cancel(self, turn_id: str, reason: str = 'admin_cancelled') -> bool:
        active = self._active.get(turn_id)
        if active is None or active[1].done():
            return False
        ctx, task = active
        ctx.cancellation_reason = reason
        task.cancel()
        return True

    @asynccontextmanager
    async def turn(self, ctx: Any):
        config = self.config()
        admission = self.capacity.admit(ctx) if self.capacity else nullcontext()
        admitted = False
        try:
            with self.track_task(ctx), admission, turn_scope(ctx.turn_id):
                admitted = True
                with self.execution_scope(ctx,config):
                    async with self._turn(ctx,config) as accepted:
                        yield accepted
        except CapacityExceeded as exc:
            if not admitted and not self.closing:
                version = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
                if await self.storage.create_turn(ctx,ctx.received.deadline,version,current_trace_id() or ''):
                    await self.storage.set_turn_status(ctx.turn_id,'failed',str(exc))
            raise

    @asynccontextmanager
    async def serial(self, ctx: Any):
        lock = self.lock_factory(ctx.self_id,ctx.event.scene,ctx.event.scene_id,ctx.user_id if ctx.is_group else 0)
        async with asyncio.timeout_at(min(ctx.received.queue_deadline,ctx.received.monotonic_deadline)):
            await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    @asynccontextmanager
    async def _turn(self, ctx: Any, config: Any):
        ctx.reply_settings = config.chat.model_copy(deep=True)
        ctx.reply_default_chunk_size = config.llm.chunk_size
        deadline = ctx.received.deadline
        version = self.version(config)
        claimed = await self.storage.create_turn(ctx, deadline, version, current_trace_id() or '')
        if not claimed:
            ctx.trace_event('turn', 'duplicate_or_stale', 'skipped')
            yield False
            return
        self._active[ctx.turn_id] = (ctx, asyncio.current_task())
        error = ''
        running = False
        try:
            async with asyncio.timeout_at(ctx.received.monotonic_deadline):
                async with self.serial(ctx):
                    if not ctx.data_current:
                        await self.storage.set_turn_status(ctx.turn_id, 'cancelled', 'context_cleared')
                        yield False
                        return
                    running = True
                    await self.storage.set_turn_status(ctx.turn_id, 'running')
                    yield True
        except TimeoutError:
            error = 'deadline_exceeded' if running else 'queue_expired'
            raise
        except asyncio.CancelledError:
            error = ctx.cancellation_reason or 'cancelled'
            raise
        except BaseException:
            error = 'execution_failed'
            raise
        finally:
            self._active.pop(ctx.turn_id, None)
            parts = await self.storage.turn_delivery_state(ctx.turn_id)
            if 'uncertain' in parts or 'sending' in parts:
                status, reason = 'delivery_uncertain', 'delivery_uncertain'
            elif not ctx.data_current:
                status, reason = 'cancelled', ctx.cancellation_reason or 'context_cleared'
            elif error:
                status = 'expired' if error in ('deadline_exceeded','queue_expired') else ('cancelled' if error in ('cancelled', 'admin_cancelled', 'account_disabled') else 'failed')
                reason = ctx.turn_reason or error if error == 'execution_failed' else error
                if error == 'execution_failed' and ('failed' in parts or 'pending' in parts):
                    reason = 'partial_delivery' if 'sent' in parts else 'delivery_failed'
            elif 'failed' in parts or 'pending' in parts:
                status, reason = 'failed', 'partial_delivery' if 'sent' in parts else 'delivery_failed'
            elif 'sent' in parts:
                status, reason = 'delivered', ctx.turn_reason
            elif ctx.turn_reason:
                status, reason = 'failed', ctx.turn_reason
            elif parts:
                status, reason = 'cancelled', 'delivery_cancelled'
            elif ctx.entrypoint == 'admin_debug':
                status, reason = 'silent', 'admin_debug_completed'
            else:
                status, reason = 'silent', ctx.turn_silent_reason or 'no_reply'
            await self.storage.set_turn_status(ctx.turn_id, status, reason)
            if self.after_turn is not None and not error:
                self.after_turn(ctx)
