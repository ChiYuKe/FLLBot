"""事件分发：按优先级匹配处理器，处理权限、限流与异常隔离。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from .onebot.models import Event, MessageEvent, event_kind
from .plugin.base import HandlerSpec
from .plugin.context import Context

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
        self._tasks: set[asyncio.Task[Any]] = set()
        self._semaphore = asyncio.Semaphore(max(1, int(bot.config.plugin.max_concurrency)))
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
    def emit(self, event: Event, connection: OneBotConnection) -> None:
        """异步派发（不阻塞 WS 读取循环）。"""
        kind = event_kind(event)
        if kind is None:
            return
        self.stats[kind] += 1
        task = asyncio.create_task(self._dispatch_guarded(event, connection, kind))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dispatch_guarded(self, event: Event, connection: OneBotConnection, kind: str) -> None:
        try:
            async with self._semaphore:
                await self.dispatch(event, connection, kind)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("事件处理失败: %s", kind)

    async def dispatch(self, event: Event, connection: OneBotConnection, kind: str | None = None) -> None:
        """同步执行所有匹配的处理器（测试里可直接调用）。"""
        kind = kind or event_kind(event) or ""
        if not self._handlers.get(kind):
            return

        permissions = self.bot.permissions
        if not await self._scene_allowed(event):
            return

        ctx = Context(bot=self.bot, connection=connection, event=event)
        for spec in list(self._handlers.get(kind, [])):
            plugin = self.bot.plugins.get(spec.plugin_name)
            if plugin is None:
                continue

            group_id = getattr(event, "group_id", None)
            if not permissions.plugin_enabled(spec.plugin_name, group_id):
                continue
            if spec.only_to_me and not ctx.to_me:
                continue

            if spec.rule is not None:
                try:
                    matched = spec.rule(ctx)
                    if inspect.isawaitable(matched):
                        matched = await matched
                except Exception:
                    self.logger.exception("规则匹配异常：%s", spec.label)
                    continue
                if not matched:
                    continue

            if not permissions.check(spec.permission, ctx):
                self.logger.debug("权限不足，跳过 %s（用户 %s）", spec.label, ctx.user_id)
                continue

            if spec.cooldown > 0 and not permissions.cooldown_ok(
                (spec.plugin_name, spec.name, ctx.user_id), spec.cooldown
            ):
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
            result = spec.callback(ctx)
            if inspect.isawaitable(result):
                if timeout > 0:
                    await asyncio.wait_for(result, timeout=timeout)
                else:
                    await result
        except TimeoutError:
            self.stats["timeouts"] += 1
            self.logger.warning("处理器超时（%.0fs）：%s", timeout, spec.label)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.stats["errors"] += 1
            self.logger.exception("处理器异常：%s", spec.label)
            await self._reply_error(ctx)

    async def _reply_error(self, ctx: Context) -> None:
        text = self.bot.config.bot.error_reply
        if not text:
            return
        try:
            await ctx.reply(text)
        except Exception:  # pragma: no cover
            self.logger.debug("错误提示发送失败", exc_info=True)

    async def drain(self, timeout: float = 5.0) -> None:
        """等待正在执行的处理器结束（关闭时用）。"""
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*list(self._tasks), return_exceptions=True), timeout=timeout)
        except TimeoutError:
            for task in list(self._tasks):
                task.cancel()

    def summary(self) -> dict[str, Any]:
        return {
            "handlers": {kind: len(bucket) for kind, bucket in self._handlers.items()},
            "startup_hooks": len(self._startup),
            "shutdown_hooks": len(self._shutdown),
            "stats": dict(self.stats),
            "pending": len(self._tasks),
        }


__all__ = ["DISPATCH_KINDS", "Dispatcher"]
