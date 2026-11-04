"""事件分发：按优先级匹配处理器，处理权限、限流与异常隔离。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .conversation import CapacityExceeded, ConversationKey, MessageEnvelope, ReceiveSnapshot, receive_scope
from .event_queue import ConversationQueue
from .onebot.models import Event, MessageEvent, event_kind
from .plugin.base import HandlerSpec
from .plugin.context import Context
from .tracing import MessageTrace, activate, record, span

if TYPE_CHECKING:  # pragma: no cover
    from .app import FLLBot
    from .onebot.connection import OneBotConnection

DISPATCH_KINDS = ("message", "notice", "request", "meta_event")


class Dispatcher:
    """把事件派发给插件处理器。"""

    def __init__(self, bot: FLLBot) -> None:
        self.bot = bot
        self.logger = logging.getLogger("fllbot.dispatcher")
        self._handlers: dict[str, list[HandlerSpec]] = {kind: [] for kind in DISPATCH_KINDS}
        self._startup: list[HandlerSpec] = []
        self._shutdown: list[HandlerSpec] = []
        self._queue = ConversationQueue(max(1, int(bot.config.plugin.max_pending_events)),
                                        bot.config.plugin.max_pending_per_conversation)
        self._workers: set[asyncio.Task[None]] = set()
        self._worker_count = max(1, int(bot.config.plugin.max_concurrency))
        self._running = 0
        self._accepting = True
        self._last_overflow_log = 0.0
        self.stats: Counter[str] = Counter()

    # -- 注册 ------------------------------------------------------------- #
    def register(self, spec: HandlerSpec) -> None:
        if spec.kind == "startup":
            self._startup.append(spec)
            self._startup.sort(key=lambda item: item.priority)
            return
        if spec.kind == "shutdown":
            self._shutdown.append(spec)
            self._shutdown.sort(key=lambda item: item.priority)
            return
        bucket = self._handlers.setdefault(spec.kind, [])
        bucket.append(spec)
        bucket.sort(key=lambda item: (item.priority, item.plugin_name, item.name))

    def unregister_plugin(self, plugin_name: str) -> int:
        removed = 0
        for kind, bucket in self._handlers.items():
            before = len(bucket)
            self._handlers[kind] = [spec for spec in bucket if spec.plugin_name != plugin_name]
            removed += before - len(self._handlers[kind])
        for attr in ("_startup", "_shutdown"):
            bucket = getattr(self, attr)
            before = len(bucket)
            setattr(self, attr, [spec for spec in bucket if spec.plugin_name != plugin_name])
            removed += before - len(getattr(self, attr))
        return removed

    def handlers(self, kind: str | None = None) -> list[HandlerSpec]:
        if kind is None:
            result: list[HandlerSpec] = []
            for bucket in self._handlers.values():
                result.extend(bucket)
            return result
        return list(self._handlers.get(kind, []))

    def command_handlers(self) -> list[HandlerSpec]:
        """所有带 usage 的消息处理器，供 /help 使用。"""
        return [spec for spec in self._handlers.get("message", []) if spec.usage]

    # -- 生命周期 --------------------------------------------------------- #
    async def startup(self) -> None:
        for spec in self._startup:
            await self._run_hook(spec, "startup")

    def start(self) -> None:
        """Start a fixed worker pool before the OneBot listener accepts events."""
        self._accepting = True
        live = {task for task in self._workers if not task.done()}
        self._workers = live
        for index in range(self._worker_count - len(live)):
            task = asyncio.create_task(self._worker(), name=f"fllbot-event-worker-{index + len(live) + 1}")
            self._workers.add(task)

    def stop_accepting(self) -> None:
        self._accepting = False

    async def shutdown(self) -> None:
        for spec in self._shutdown:
            await self._run_hook(spec, "shutdown")

    async def _run_hook(self, spec: HandlerSpec, label: str) -> None:
        try:
            result = spec.callback(self.bot)
            if inspect.isawaitable(result):
                await result
        except Exception:
            self.logger.exception("%s 钩子执行失败: %s", label, spec.label)

    # -- 派发 ------------------------------------------------------------- #
    def _memory_snapshot(self, event: Event, connection: OneBotConnection, snapshot: ReceiveSnapshot) -> ReceiveSnapshot:
        if isinstance(event,MessageEvent) and snapshot.memory_allowed is None:
            with receive_scope(snapshot):
                ctx = Context(self.bot,connection,event)
            return ctx.received
        return snapshot

    async def enqueue(self, event: Event, connection: OneBotConnection, *, trace: MessageTrace | None = None) -> None:
        if not self._accepting:
            self.emit(event,connection,trace=trace)
            return
        if not isinstance(event, MessageEvent):
            self.emit(event,connection,trace=trace)
            return
        event = event.model_copy(deep=True)
        envelope = MessageEnvelope.from_event(event,connection)
        snapshot = ReceiveSnapshot.capture(self.bot.storage.conversation_generation,self.bot.config.plugin,
                                           self.bot.bots.epoch(envelope.self_id),envelope.turn_id,
                                           self.bot.storage.scope_generation(envelope.self_id,envelope.scene,envelope.scene_id))
        snapshot = self._memory_snapshot(event,connection,snapshot)
        if not await self.bot.storage.receive_event(envelope,snapshot,trace.trace_id if trace else ''):
            self.stats['duplicate_persisted'] += 1
            if trace:
                trace.add('dedup','duplicate_message','skipped')
                self.bot.traces.submit(trace,'skipped')
            return
        result = self.emit(event,connection,trace=trace,snapshot=snapshot,envelope=envelope)
        await self.bot.storage.update_event(envelope.event_key,'queued' if result=='queued' else 'dropped',result or 'unsupported')

    def emit(self, event: Event, connection: OneBotConnection, *, trace: MessageTrace | None = None, snapshot: ReceiveSnapshot | None = None, envelope: MessageEnvelope | None = None) -> str | None:
        """Queue an event without blocking the WebSocket reader; reject newest on overflow."""
        kind = event_kind(event)
        if kind is None:
            return
        self.stats[kind] += 1
        if trace is None and isinstance(event, MessageEvent):
            trace = MessageTrace(event, connection)
            trace.add('receive', 'received')
        if not self._accepting:
            self.stats["rejected_during_shutdown"] += 1
            if trace:
                trace.add('queue', 'shutdown_rejected', 'skipped')
                self.bot.traces.submit(trace, 'dropped')
            return 'shutdown_rejected'
        if not self._workers:
            self.start()
        if isinstance(event,MessageEvent) and envelope is None:
            event = event.model_copy(deep=True)
        ticket = None
        try:
            if trace:
                trace.add('queue', 'queued')
            snapshot = snapshot or ReceiveSnapshot.capture(self.bot.storage.conversation_generation, self.bot.config.plugin,
                                               self.bot.bots.epoch(int(event.self_id or connection.self_id or 0)))
            if isinstance(event, MessageEvent):
                ticket = self.bot.capacity.reserve(ConversationKey('qq',int(event.self_id or connection.self_id or 0),event.scene,event.scene_id))
                snapshot = replace(snapshot,admission=ticket)
            if isinstance(event, MessageEvent) and envelope is None:
                snapshot = replace(snapshot,scope_generation=self.bot.storage.scope_generation(
                    int(event.self_id or connection.self_id or 0),event.scene,event.scene_id))
            snapshot = self._memory_snapshot(event,connection,snapshot)
            self._queue.put_nowait((event, connection, kind, trace, asyncio.get_running_loop().time(), snapshot, envelope))
            self.stats["queued"] += 1
            return 'queued'
        except (asyncio.QueueFull, CapacityExceeded):
            if ticket is not None:
                ticket.release()
            if trace:
                trace.add('queue', 'queue_overflow', 'skipped')
                self.bot.traces.submit(trace, 'dropped')
            self.stats["dropped_overflow"] += 1
            now = asyncio.get_running_loop().time()
            if now - self._last_overflow_log >= 5:
                self.logger.warning(
                    "事件队列已满，丢弃新事件（累计 %d；队列上限 %d）",
                    self.stats["dropped_overflow"], self._queue.maxsize,
                )
                self._last_overflow_log = now

        return 'queue_overflow'

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            event, connection, kind, trace, queued_at = item[:5]
            snapshot = item[5] if len(item) > 5 else ReceiveSnapshot.capture(
                self.bot.storage.conversation_generation, self.bot.config.plugin)
            envelope = item[6] if len(item)>6 else None
            async def event_status(status, reason='', envelope=envelope):
                if envelope is not None:
                    await self.bot.storage.update_event(envelope.event_key,status,reason)
            self._running += 1
            try:
                elapsed = asyncio.get_running_loop().time() - queued_at
                options = self.bot.config.plugin
                remaining = snapshot.monotonic_deadline - asyncio.get_running_loop().time()
                self_id = int(event.self_id or connection.self_id or 0)
                if (not self.bot.bots.enabled(self_id) or snapshot.account_epoch != self.bot.bots.epoch(self_id)):
                    await event_status('cancelled','account_disabled')
                    self.stats['cancelled_account'] += 1
                    if trace:
                        trace.add('queue', 'account_disabled', 'skipped')
                        self.bot.traces.submit(trace, 'dropped')
                    continue
                if (snapshot.data_epoch != self.bot.storage.conversation_generation
                        or isinstance(event,MessageEvent) and not self.bot.storage.scope_current(
                            self_id,event.scene,event.scene_id,snapshot.scope_generation)):
                    await event_status('cancelled','context_cleared')
                    self.stats['cancelled_queued'] += 1
                    if trace:
                        trace.add('queue', 'context_cleared', 'skipped')
                        self.bot.traces.submit(trace, 'dropped')
                    continue
                if (remaining <= 0 or asyncio.get_running_loop().time() >= snapshot.queue_deadline
                        or (len(item) <= 5 and elapsed >= options.queue_timeout_seconds)):
                    await event_status('expired','queue_expired')
                    self.stats['expired_queued'] += 1
                    if trace:
                        trace.add('queue', 'queue_expired', 'skipped')
                        self.bot.traces.submit(trace, 'dropped')
                    continue
                await event_status('processing')
                try:
                    with receive_scope(snapshot):
                        processed = await asyncio.wait_for(self._dispatch_guarded(event, connection, kind, trace), remaining)
                    await event_status('failed' if processed is False else 'completed')
                except asyncio.CancelledError:
                    await event_status('cancelled','dispatch_cancelled')
                    if asyncio.current_task().cancelling():
                        raise
                    self.stats['cancelled_running'] += 1
                except TimeoutError:
                    await event_status('expired','deadline_exceeded')
                    self.stats['expired_running'] += 1
            except Exception as exc:
                self.stats['worker_event_failures'] += 1
                self.logger.error('事件事实提交或处理失败，停止本条事件，继续处理队列（%s）', type(exc).__name__)
            finally:
                if snapshot.admission is not None:
                    snapshot.admission.release()
                self._running -= 1
                self._queue.task_done()

    async def _dispatch_guarded(self, event: Event, connection: OneBotConnection, kind: str, trace: MessageTrace | None = None) -> bool:
        try:
            await self.dispatch(event, connection, kind, trace=trace)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error("事件处理失败: %s（%s）", kind, type(exc).__name__)
            return False


    async def dispatch(self, event: Event, connection: OneBotConnection, kind: str | None = None, *, trace: MessageTrace | None = None) -> None:
        if trace is None and isinstance(event, MessageEvent):
            trace = MessageTrace(event, connection)
            trace.add('receive', 'received')
        with activate(trace):
            try:
                with span('dispatch'):
                    await self._dispatch(event, connection, kind)
            finally:
                if trace is not None:
                    self.bot.traces.submit(trace)

    async def _record_trigger_rejection(self, ctx: Context, reason: str) -> None:
        if isinstance(ctx.event, MessageEvent):
            decision = self.bot.trigger_policy.rejected(ctx, reason)
            ctx.trigger_decision = decision
            await self.bot.storage.record_trigger(ctx, decision)

    async def _dispatch(self, event: Event, connection: OneBotConnection, kind: str | None = None) -> None:
        """同步执行所有匹配的处理器（测试里可直接调用）。"""
        kind = kind or event_kind(event) or ""
        if not self._handlers.get(kind):
            record('plugin', 'no_handlers', 'skipped')
            return

        ctx = Context(bot=self.bot, connection=connection, event=event)
        if int(connection.self_id or 0) != ctx.self_id:
            await self._record_trigger_rejection(ctx,'account_mismatch')
            record('access','account_mismatch','skipped')
            return
        if not self.bot.bots.enabled(int(event.self_id or connection.self_id or 0)):
            await self._record_trigger_rejection(ctx, 'account_disabled')
            record('access', 'account_disabled', 'skipped')
            return
        permissions = self.bot.permissions
        if not await self._scene_allowed(event):
            await self._record_trigger_rejection(ctx, 'scene_denied')
            record('access', 'scene_denied', 'skipped')
            return
        record('access', 'scene_allowed')

        if isinstance(event, MessageEvent):
            with span('memory'):
                await self.bot.chat.memory.observe(ctx)
        for spec in list(self._handlers.get(kind, [])):
            plugin = self.bot.plugins.get(spec.plugin_name)
            if plugin is None:
                record('plugin', 'plugin_not_loaded', 'skipped', spec.plugin_name)
                continue

            group_id = getattr(event, "group_id", None)
            if not permissions.plugin_enabled(spec.plugin_name, group_id):
                if spec.plugin_name == 'agent':
                    await self._record_trigger_rejection(ctx, 'plugin_disabled')
                code = 'global_plugin_disabled' if permissions.plugin_state(spec.plugin_name) is False else 'plugin_disabled'
                record('plugin', code, 'skipped', spec.plugin_name)
                continue
            if spec.only_to_me and not ctx.to_me:
                record('trigger', 'at_required', 'skipped', spec.label)
                continue

            if spec.rule is not None:
                try:
                    matched = spec.rule(ctx)
                    if inspect.isawaitable(matched):
                        matched = await matched
                except Exception as exc:
                    record('rule', 'rule_failed', 'failed', spec.label)
                    self.logger.error("规则匹配异常：%s（%s）", spec.label, type(exc).__name__)
                    continue
                if not matched:
                    record('rule', 'rule_not_matched', 'skipped', spec.label)
                    continue

            if not permissions.check(spec.permission, ctx):
                record('permission', 'user_not_allowed', 'skipped', spec.label)
                self.logger.debug("权限不足，跳过 %s（用户 %s）", spec.label, ctx.user_id)
                continue

            if spec.cooldown > 0 and not permissions.cooldown_ok(
                (spec.plugin_name, spec.name, ctx.user_id), spec.cooldown
            ):
                record('cooldown', 'handler_cooldown', 'skipped', spec.label)
                continue

            ctx.plugin = plugin
            ctx.handler = spec
            await self._invoke(spec, ctx)
            ctx.plugin = None
            ctx.handler = None

            if ctx.stopped or spec.block:
                break

        self.stats["handled"] += 1

    async def _scene_allowed(self, event: Event) -> bool:
        """群开关 / 私聊开关的总闸。"""
        permissions = self.bot.permissions
        if isinstance(event, MessageEvent):
            if event.is_group and event.group_id is not None:
                await permissions.ensure_group(event.group_id)
                return permissions.group_enabled(event.group_id)
            return permissions.allow_private
        group_id = getattr(event, "group_id", None)
        if group_id:
            return permissions.group_enabled(group_id)
        return True

    async def _invoke(self, spec: HandlerSpec, ctx: Context) -> None:
        timeout = float(self.bot.config.plugin.handler_timeout or 0)
        try:
            with span('handler', spec.label):
                result = spec.callback(ctx)
                if inspect.isawaitable(result):
                    if timeout > 0:
                        await asyncio.wait_for(result, timeout=timeout)
                    else:
                        await result
        except TimeoutError:
            record('handler', 'handler_timeout', 'failed', spec.label)
            self.stats["timeouts"] += 1
            self.logger.warning("处理器超时（%.0fs）：%s", timeout, spec.label)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record('handler', 'handler_failed', 'failed', spec.label)
            self.stats["errors"] += 1
            self.logger.error("处理器异常：%s（%s）", spec.label,type(exc).__name__)
            await self._reply_error(ctx)

    async def _reply_error(self, ctx: Context) -> None:
        text = self.bot.config.bot.error_reply
        if not text:
            return
        try:
            await ctx.reply(text)
        except Exception as exc:  # pragma: no cover
            self.logger.debug("错误提示发送失败（%s）", type(exc).__name__)

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait for queued events, then stop workers; cancel remaining work after the deadline."""
        self.stop_accepting()
        if self._workers:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=max(0.0, timeout))
            except TimeoutError:
                pending = self._queue.qsize()
                self.stats["dropped_shutdown"] += pending
                for item in self._queue.discard_pending():
                    trace = item[3]
                    if len(item)>5 and item[5].admission is not None:
                        item[5].admission.release()
                    if len(item)>6 and item[6] is not None:
                        await self.bot.storage.update_event(item[6].event_key,'cancelled','shutdown_dropped')
                    if trace:
                        trace.add('queue', 'shutdown_dropped', 'skipped')
                        self.bot.traces.submit(trace, 'dropped')
                self.logger.warning(
                    "等待事件处理超过 %.1f 秒，取消剩余任务（排队丢弃 %d，正在处理 %d）",
                    timeout, pending, self._running,
                )
            workers = list(self._workers)
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self._workers.clear()

    def summary(self) -> dict[str, Any]:
        return {
            "handlers": {kind: len(bucket) for kind, bucket in self._handlers.items()},
            "startup_hooks": len(self._startup),
            "shutdown_hooks": len(self._shutdown),
            "stats": dict(self.stats),
            "accepting": self._accepting,
            "pending": self._queue.qsize(),
            "running": self._running,
            "workers": len(self._workers),
            "queue_capacity": self._queue.maxsize,
            "conversation_admission": self.bot.capacity.status(),
        }


__all__ = ["DISPATCH_KINDS", "Dispatcher"]
