"""任务内上下文：当前正在处理的入站消息正文，只供运行日志展示，不写入数据库。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_text: ContextVar[str | None] = ContextVar("fllbot_inbound_message", default=None)


@contextmanager
def inbound_message(text: str | None) -> Iterator[None]:
    """本轮消息处理期间暴露一条可读的入站消息。"""
    value = (text or "").strip()
    if not value:
        yield
        return
    token = _text.set(value)
    try:
        yield
    finally:
        _text.reset(token)


def current_inbound_message() -> str | None:
    """当前任务正在处理的入站消息；没有时返回 ``None``。"""
    return _text.get()
