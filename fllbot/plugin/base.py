"""插件基类与处理器声明。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from ..rules import Rule, evaluate
from ..rules import command as command_rule
from .settings import PluginSettings, SettingField

HandlerKind = Literal["message", "notice", "request", "meta_event", "startup", "shutdown"]

HandlerCallback = Callable[[Any], Awaitable[Any]]
LoopCallback = Callable[[Any], Awaitable[Any]]


@dataclass
class HandlerSpec:
    """一个事件处理器的声明。"""

    kind: HandlerKind
    callback: HandlerCallback
    name: str
    plugin_name: str = ""
    priority: int = 10
    block: bool = False
    rule: Rule | None = None
    permission: str | None = None
    cooldown: float = 0.0
    only_to_me: bool = False
    # 供 /help 展示的用法说明，例如 "/ping"
    usage: str = ""

    @property
    def label(self) -> str:
        return f"{self.plugin_name}:{self.name}" if self.plugin_name else self.name

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "usage": self.usage,
            "priority": self.priority,
            "block": self.block,
            "permission": self.permission,
            "cooldown": self.cooldown,
            "only_to_me": self.only_to_me,
        }


@dataclass
class LoopSpec:
    """周期性任务声明。"""

    callback: LoopCallback
    seconds: float
    name: str
    initial_delay: float = 0.0


class Plugin:
    """插件基类。

    两种写法都支持::

        # 1) 直接实例化
        plugin = Plugin(name="ping", description="最小示例")

        # 2) 子类化
        class PingPlugin(Plugin):
            name = "ping"
    """

    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    enabled_by_default: bool = True

    def __init__(
        self,
        name: str | None = None,
        *,
        version: str | None = None,
        description: str | None = None,
        author: str | None = None,
        enabled_by_default: bool | None = None,
    ) -> None:
        if name:
            self.name = name
        if version is not None:
            self.version = version
        if description is not None:
            self.description = description
        if author is not None:
            self.author = author
        if enabled_by_default is not None:
            self.enabled_by_default = enabled_by_default

        self._handlers: list[HandlerSpec] = []
        self._loops: list[LoopSpec] = []
        self._settings = PluginSettings(lambda: self.name)
        self.bot: Any = None
        self.module: Any = None
        self.path: Any = None
        # 插件自带的说明文档（Markdown）。可以在代码里直接赋值，或由加载器
        # 从插件目录的 README.md / 同名 .md 读取。
        self.readme: str = ""
        self.logger = logging.getLogger(f"fllbot.plugin.{self.name or 'unnamed'}")

    # -- 元信息 ----------------------------------------------------------- #
    def __repr__(self) -> str:  # pragma: no cover
        return f"<Plugin {self.name!r} v{self.version}>"

    @property
    def handlers(self) -> tuple[HandlerSpec, ...]:
        return tuple(self._handlers)

    @property
    def loops(self) -> tuple[LoopSpec, ...]:
        return tuple(self._loops)

    @property
    def settings(self) -> PluginSettings:
        return self._settings

    def setting(
        self,
        key: str,
        *,
        type: str = "string",
        label: str = "",
        default: Any = None,
        description: str = "",
        options: tuple[Any, ...] | list[Any] = (),
        min: float | None = None,
        max: float | None = None,
        secret: bool = False,
        group_scoped: bool = False,
    ) -> SettingField:
        """声明一个可被管理端渲染与修改的配置项。"""
        return self._settings.define(
            SettingField(
                key=key,
                type=type,
                label=label,
                default=default,
                description=description,
                options=tuple(options),
                minimum=min,
                maximum=max,
                secret=secret,
                group_scoped=group_scoped,
            )
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "enabled_by_default": self.enabled_by_default,
            "handlers": [spec.describe() for spec in self._handlers],
            "loops": [{"name": spec.name, "seconds": spec.seconds} for spec in self._loops],
            "settings": self._settings.schema(),
            "has_settings": bool(self._settings.fields),
            "has_readme": bool(self.readme.strip()),
            "path": str(self.path) if self.path else None,
        }

    # -- 注册 ------------------------------------------------------------- #
    def add_handler(self, spec: HandlerSpec) -> HandlerSpec:
        if not self.name:
            raise ValueError("插件必须先有 name 才能注册处理器")
        spec.plugin_name = self.name
        self._handlers.append(spec)
        return spec

    def _decorate(self, spec_factory: Callable[[Callable[..., Any]], HandlerSpec]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.add_handler(spec_factory(func))
            return func

        return decorator

    # -- 事件装饰器 ------------------------------------------------------- #
    def on_message(
        self,
        *,
        rule: Rule | None = None,
        priority: int = 20,
        block: bool = False,
        permission: str | None = None,
        cooldown: float = 0.0,
        only_to_me: bool = False,
        name: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """任意消息（含非命令文本）。"""

        return self._decorate(
            lambda func: HandlerSpec(
                kind="message",
                callback=func,
                name=name or func.__name__,
                rule=rule,
                priority=priority,
                block=block,
                permission=permission,
                cooldown=cooldown,
                only_to_me=only_to_me,
            )
        )

    def on_command(
        self,
        *names: str,
        aliases: tuple[str, ...] | list[str] = (),
        prefix: list[str] | None = None,
        priority: int = 10,
        block: bool = True,
        permission: str | None = None,
        cooldown: float = 0.0,
        only_to_me: bool = False,
        ignore_case: bool = True,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """命令处理器，默认命中后阻断后续处理器。"""
        if not names:
            raise ValueError("on_command() 至少需要一个命令名")
        rule = command_rule(*names, aliases=aliases, prefixes=prefix, ignore_case=ignore_case)
        usage = "|".join((*names, *aliases))

        return self._decorate(
            lambda func: HandlerSpec(
                kind="message",
                callback=func,
                name=names[0],
                rule=rule,
                priority=priority,
                block=block,
                permission=permission,
                cooldown=cooldown,
                only_to_me=only_to_me,
                usage=usage,
            )
        )

    def on_notice(
        self,
        *notice_types: str,
        rule: Rule | None = None,
        priority: int = 15,
        block: bool = False,
        permission: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """通知事件，例如 ``group_increase`` / ``group_ban``。"""
        type_rule = _type_rule("notice_type", notice_types)
        combined = _combine(rule, type_rule)
        return self._decorate(
            lambda func: HandlerSpec(
                kind="notice",
                callback=func,
                name=func.__name__,
                rule=combined,
                priority=priority,
                block=block,
                permission=permission,
            )
        )

    def on_request(
        self,
        *request_types: str,
        rule: Rule | None = None,
        priority: int = 15,
        block: bool = False,
        permission: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """请求事件，例如 ``friend`` / ``group``。"""
        type_rule = _type_rule("request_type", request_types)
        combined = _combine(rule, type_rule)
        return self._decorate(
            lambda func: HandlerSpec(
                kind="request",
                callback=func,
                name=func.__name__,
                rule=combined,
                priority=priority,
                block=block,
                permission=permission,
            )
        )

    def on_meta_event(
        self,
        *meta_types: str,
        rule: Rule | None = None,
        priority: int = 50,
        block: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """元事件（心跳、生命周期）。"""
        type_rule = _type_rule("meta_event_type", meta_types)
        combined = _combine(rule, type_rule)
        return self._decorate(
            lambda func: HandlerSpec(
                kind="meta_event",
                callback=func,
                name=func.__name__,
                rule=combined,
                priority=priority,
                block=block,
            )
        )

    def on_startup(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """启动钩子，回调签名为 ``async def hook(bot)``。"""
        return self._decorate(
            lambda func: HandlerSpec(kind="startup", callback=func, name=func.__name__, priority=0, block=False)
        )

    def on_shutdown(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """关闭钩子，回调签名为 ``async def hook(bot)``。"""
        return self._decorate(
            lambda func: HandlerSpec(kind="shutdown", callback=func, name=func.__name__, priority=0, block=False)
        )

    def on_loop(
        self,
        seconds: float,
        *,
        name: str | None = None,
        initial_delay: float = 0.0,
    ) -> Callable[[LoopCallback], LoopCallback]:
        """周期任务，回调签名为 ``async def tick(bot)``。"""
        if seconds <= 0:
            raise ValueError("on_loop 的间隔必须大于 0")

        def decorator(func: LoopCallback) -> LoopCallback:
            self._loops.append(
                LoopSpec(callback=func, seconds=float(seconds), name=name or func.__name__, initial_delay=initial_delay)
            )
            return func

        return decorator

    # -- 生命周期 --------------------------------------------------------- #
    async def on_load(self, bot: Any) -> None:
        """插件加载完成后调用，可在此初始化资源。"""

    async def on_unload(self, bot: Any) -> None:
        """插件卸载前调用，可在此释放资源。"""


def _type_rule(attr: str, types: tuple[str, ...]) -> Rule | None:
    if not types:
        return None

    def _rule(ctx: Any) -> bool:
        return getattr(ctx.event, attr, "") in types

    return _rule


def _combine(rule: Rule | None, extra: Rule | None) -> Rule | None:
    if rule is None:
        return extra
    if extra is None:
        return rule

    async def _rule(ctx: Any) -> bool:
        return await evaluate(rule, ctx) and await evaluate(extra, ctx)

    return _rule


__all__ = ["HandlerKind", "HandlerSpec", "LoopSpec", "Plugin"]
