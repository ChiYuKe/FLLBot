"""插件基类与处理器声明。"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..rules import Rule, evaluate
from ..rules import command as command_rule
from .settings import PluginSettings, SettingField

HandlerKind = Literal["message", "notice", "request", "meta_event", "startup", "shutdown"]

HandlerCallback = Callable[[Any], Awaitable[Any]]
LoopCallback = Callable[[Any], Awaitable[Any]]
AIToolCallback = Callable[..., Awaitable[Any]]


@dataclass
class AIToolSpec:
    """显式开放给 AI 智能体调用的插件工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    callback: AIToolCallback
    permission: str | None = None
    plugin_name: str = ""
    # 面向管理端展示的中文名称，决定工具叫什么的是插件，而不是模型看到的 name。
    title: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin_name}__{self.name}"

    @property
    def display_title(self) -> str:
        return self.title.strip() or self.name


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
    category: str = "plugin"

    def __init__(
        self,
        name: str | None = None,
        *,
        version: str | None = None,
        description: str | None = None,
        author: str | None = None,
        enabled_by_default: bool | None = None,
        category: str | None = None,
    ) -> None:
        if category is not None:
            self.category = category
        if self.category not in {"plugin", "tool"}:
            raise ValueError("扩展类别必须为 plugin 或 tool")
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
        self._ai_tools: list[AIToolSpec] = []
        self._settings = PluginSettings(lambda: self.name)
        self.bot: Any = None
        self.module: Any = None
        self.path: Any = None
        # 插件自带的说明文档（Markdown）。可以在代码里直接赋值，或由加载器
        # 从插件目录的 README.md / 同名 .md 读取。
        self.readme: str = ""
        self.logger = logging.getLogger(f"fllbot.plugin.{self.name or 'unnamed'}")
        self._cleanups: list[Callable[[], Any]] = []

    @classmethod
    def from_manifest(cls, location: str | Path) -> Plugin:
        """从当前包的 plugin.toml 创建实例，避免在代码中重复维护包头。"""
        from .manifest import PluginManifest

        path = Path(location).resolve()
        manifest = PluginManifest.read(path if path.is_dir() else path.parent)
        return cls(
            name=manifest.name,
            version=manifest.version,
            description=manifest.description,
            author=manifest.author,
            category=manifest.category,
        )

    @property
    def data_dir(self) -> Path:
        """插件持久化文件目录，与代码分离；KV 配置仍使用原有 storage。"""
        from .manifest import validate_name

        if self.bot is None:
            raise RuntimeError("插件尚未加载，无法访问 data_dir")
        validate_name(self.name)
        path = self.bot.root / "data" / "plugins" / self.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def on_cleanup(self, callback: Callable[[], Any]) -> None:
        """登记同步或异步清理函数，卸载和初始化失败时逆序执行。"""
        if not callable(callback):
            raise TypeError("清理回调必须可调用")
        self._cleanups.append(callback)

    async def cleanup(self) -> None:
        callbacks, self._cleanups = self._cleanups, []
        for callback in reversed(callbacks):
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                self.logger.exception("插件资源清理失败")

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
    def ai_tools(self) -> tuple[AIToolSpec, ...]:
        return tuple(self._ai_tools)

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
            "category": self.category,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "enabled_by_default": self.enabled_by_default,
            "handlers": [spec.describe() for spec in self._handlers],
            "loops": [{"name": spec.name, "seconds": spec.seconds} for spec in self._loops],
            "ai_tools": [
                {
                    "name": spec.qualified_name,
                    "title": spec.display_title,
                    "description": spec.description,
                    "permission": spec.permission,
                }
                for spec in self._ai_tools
            ],
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

    def on_ai_tool(
        self,
        name: str,
        *,
        description: str,
        title: str = "",
        parameters: dict[str, Any] | None = None,
        permission: str | None = None,
    ) -> Callable[[AIToolCallback], AIToolCallback]:
        """显式注册一个 AI 可调用工具；未声明的插件能力不会暴露给模型。

        ``name`` 是模型看到的工具名（最终为 ``插件名__工具名``）；``title`` 是管理端
        展示用的名称，例如「联网搜索」。回调签名为 ``async def tool(ctx, **arguments)``，
        参数定义使用 JSON Schema。
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("AI 工具名只能包含 ASCII 字母、数字、下划线和连字符")
        if not description.strip():
            raise ValueError("AI 工具必须提供 description")
        schema = parameters or {"type": "object", "properties": {}, "additionalProperties": False}
        if schema.get("type") != "object":
            raise ValueError("AI 工具 parameters 必须是 object schema")

        def decorator(func: AIToolCallback) -> AIToolCallback:
            if any(item.name == name for item in self._ai_tools):
                raise ValueError(f"AI 工具重复注册：{name}")
            self._ai_tools.append(
                AIToolSpec(
                    name=name,
                    description=description,
                    parameters=dict(schema),
                    callback=func,
                    permission=permission,
                    plugin_name=self.name,
                    title=title.strip(),
                )
            )
            return func

        return decorator

    def _decorate(
        self, spec_factory: Callable[[Callable[..., Any]], HandlerSpec]
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
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


__all__ = ["AIToolSpec", "HandlerKind", "HandlerSpec", "LoopSpec", "Plugin"]
