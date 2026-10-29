"""事件匹配规则（谓词），既支持同步也支持异步。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from re import Pattern
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .plugin.context import Context

Rule = Callable[["Context"], bool | Awaitable[bool]]


async def evaluate(rule: Rule, ctx: Context) -> bool:
    """执行规则并统一成布尔值（兼容同步/异步规则）。"""
    return bool(await _maybe_await(rule(ctx)))


def all_rules(*rules: Rule) -> Rule:
    """全部满足才算命中（短路）。"""

    async def _rule(ctx: Context) -> bool:
        for rule in rules:
            if not await evaluate(rule, ctx):
                return False
        return True

    return _rule


def any_rule(*rules: Rule) -> Rule:
    """任一满足即命中。"""

    async def _rule(ctx: Context) -> bool:
        for rule in rules:
            if await evaluate(rule, ctx):
                return True
        return False

    return _rule


def not_rule(rule: Rule) -> Rule:
    async def _rule(ctx: Context) -> bool:
        return not await evaluate(rule, ctx)

    return _rule


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #


def to_me() -> Rule:
    """私聊，或群聊里 @ 了机器人。"""

    def _rule(ctx: Context) -> bool:
        return bool(ctx.to_me)

    return _rule


def in_group(group_id: int | None = None) -> Rule:
    def _rule(ctx: Context) -> bool:
        if not ctx.is_group:
            return False
        return group_id is None or ctx.group_id == group_id

    return _rule


def in_private() -> Rule:
    def _rule(ctx: Context) -> bool:
        return ctx.is_private

    return _rule


# --------------------------------------------------------------------------- #
# 文本匹配
# --------------------------------------------------------------------------- #


def _text_of(ctx: Context) -> str:
    return ctx.text if ctx.text else ctx.readable


def keyword(*words: str, ignore_case: bool = True) -> Rule:
    """文本包含任一关键词。"""

    def _rule(ctx: Context) -> bool:
        text = _text_of(ctx)
        if not text:
            return False
        haystack = text.lower() if ignore_case else text
        return any((word.lower() if ignore_case else word) in haystack for word in words)

    return _rule


def startswith(*prefixes: str, ignore_case: bool = False) -> Rule:
    def _rule(ctx: Context) -> bool:
        text = ctx.text
        if not text:
            return False
        if ignore_case:
            lowered = text.lower()
            return any(lowered.startswith(prefix.lower()) for prefix in prefixes)
        return any(text.startswith(prefix) for prefix in prefixes)

    return _rule


def endswith(*suffixes: str, ignore_case: bool = False) -> Rule:
    def _rule(ctx: Context) -> bool:
        text = ctx.text
        if not text:
            return False
        if ignore_case:
            lowered = text.lower()
            return any(lowered.endswith(suffix.lower()) for suffix in suffixes)
        return any(text.endswith(suffix) for suffix in suffixes)

    return _rule


def regex(pattern: str | Pattern[str], flags: int = 0) -> Rule:
    compiled = re.compile(pattern, flags) if isinstance(pattern, str) else pattern

    def _rule(ctx: Context) -> bool:
        match = compiled.search(ctx.text)
        if match is None:
            return False
        ctx.regex_match = match
        return True

    return _rule


def fullmatch(pattern: str | Pattern[str], flags: int = 0) -> Rule:
    compiled = re.compile(pattern, flags) if isinstance(pattern, str) else pattern

    def _rule(ctx: Context) -> bool:
        match = compiled.fullmatch(ctx.text)
        if match is None:
            return False
        ctx.regex_match = match
        return True

    return _rule


def command(
    *names: str,
    aliases: Iterable[str] = (),
    prefixes: list[str] | None = None,
    ignore_case: bool = True,
) -> Rule:
    """命令匹配：解析出命令名与参数写入 ``ctx.command / args / argv``。

    识别三种写法：``/ping``（配置前缀）、``@机器人 ping``（被 @ 时允许裸命令）、
    以及配置里加入 ``""`` 前缀后的纯裸命令。
    """
    if not names:
        raise ValueError("command() 至少需要一个命令名")
    wanted = {_normalize(name, ignore_case) for name in (*names, *aliases)}

    def _rule(ctx: Context) -> bool:
        text = ctx.text
        if not text:
            return False

        configured = ctx.config.bot.command_prefix
        prefix_list = list(prefixes) if prefixes is not None else list(configured)
        candidates: list[str] = []

        for prefix in prefix_list:
            if prefix and text.startswith(prefix):
                candidates.append(text[len(prefix) :].lstrip())
            elif not prefix and (ctx.to_me or ctx.config.bot.allow_bare_command):
                candidates.append(text)
        if ctx.to_me:
            candidates.append(text)

        for candidate in candidates:
            candidate = candidate.lstrip()
            if not candidate:
                continue
            head, _, rest = candidate.partition(" ")
            head = head.strip()
            if not head:
                continue
            if _normalize(head, ignore_case) in wanted:
                ctx.command = _normalize(head, ignore_case)
                ctx.args = rest.strip()
                ctx.argv = ctx.args.split()
                return True
        return False

    return _rule


def _normalize(value: str, ignore_case: bool) -> str:
    return value.lower() if ignore_case else value


__all__ = [
    "Rule",
    "all_rules",
    "any_rule",
    "command",
    "endswith",
    "fullmatch",
    "in_group",
    "in_private",
    "keyword",
    "not_rule",
    "regex",
    "startswith",
    "to_me",
]
