"""处理器上下文：把事件、连接和机器人能力包在一起递给插件。"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..onebot.models import Event, MessageEvent, Sender
from ..onebot.segments import (
    MessageSegment,
    as_segments,
    plain_text,
    readable_text,
    strip_leading_at_self,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..onebot.connection import OneBotConnection
    from .base import HandlerSpec, Plugin

_logger = logging.getLogger("fllbot.context")


@dataclass
class Context:
    """一次事件处理过程中的上下文对象。"""

    bot: Any
    connection: OneBotConnection
    event: Event
    plugin: Plugin | None = None
    handler: HandlerSpec | None = None
    # 命令解析结果
    command: str | None = None
    args: str = ""
    argv: list[str] = field(default_factory=list)
    # 供插件主动中断后续处理器
    stopped: bool = False
    # regex 规则命中的 Match 对象
    regex_match: re.Match[str] | None = None
    _text_cache: str | None = field(default=None, repr=False)

    # -- 基础属性 --------------------------------------------------------- #
    @property
    def self_id(self) -> int:
        return int(self.event.self_id or self.connection.self_id or 0)

    @property
    def message(self) -> list[MessageSegment]:
        return list(self.event.message) if isinstance(self.event, MessageEvent) else []

    @property
    def message_type(self) -> str:
        return getattr(self.event, "message_type", "")

    @property
    def is_group(self) -> bool:
        """群消息，或带群号的群通知/请求。"""
        if isinstance(self.event, MessageEvent):
            return self.event.is_group
        return getattr(self.event, "group_id", None) is not None

    @property
    def is_private(self) -> bool:
        return isinstance(self.event, MessageEvent) and self.event.is_private

    @property
    def group_id(self) -> int | None:
        return getattr(self.event, "group_id", None)

    @property
    def user_id(self) -> int:
        return int(getattr(self.event, "user_id", 0) or 0)

    @property
    def message_id(self) -> int:
        return int(getattr(self.event, "message_id", 0) or 0)

    @property
    def sender(self) -> Sender:
        sender = getattr(self.event, "sender", None)
        return sender if isinstance(sender, Sender) else Sender()

    @property
    def nickname(self) -> str:
        if isinstance(self.event, MessageEvent):
            return self.event.nickname
        return self.sender.card or self.sender.nickname or str(self.user_id)

    @property
    def to_me(self) -> bool:
        return bool(getattr(self.event, "to_me", False))

    @property
    def raw_text(self) -> str:
        text = plain_text(self.message)
        return text.strip() if text else ""

    @property
    def text(self) -> str:
        """去掉开头 ``@机器人`` 后的纯文本，命令解析与匹配都用它。"""
        if self._text_cache is None:
            segments = strip_leading_at_self(self.message, self.self_id)
            self._text_cache = plain_text(segments).strip()
        return self._text_cache

    @property
    def readable(self) -> str:
        return readable_text(self.message).strip()

    # -- 环境 ------------------------------------------------------------- #
    @property
    def config(self) -> Any:
        return self.bot.config

    @property
    def db(self) -> Any:
        return self.bot.storage

    @property
    def data_dir(self) -> Path:
        """当前插件的文件数据目录；源码目录不用于保存运行数据。"""
        if self.plugin is None:
            raise RuntimeError("当前上下文没有关联插件")
        return self.plugin.data_dir

    @property
    def llm(self) -> Any:
        return self.bot.chat

    @property
    def settings(self) -> Any:
        """当前插件的配置视图；群级字段自动读取本群覆盖值。"""
        if self.plugin is None:
            raise RuntimeError("当前上下文没有关联插件，无法读取配置")
        return self.plugin.settings.view(self.group_id if self.is_group else None)

    @property
    def logger(self) -> logging.Logger:
        return self.plugin.logger if self.plugin is not None else _logger

    @property
    def is_superuser(self) -> bool:
        return self.bot.permissions.is_superuser(self.user_id)

    @property
    def is_group_admin(self) -> bool:
        if self.is_superuser:
            return True
        return self.sender.role in {"owner", "admin"}

    # -- 发送 ------------------------------------------------------------- #
    async def send(
        self,
        message: str | Sequence[MessageSegment] | None,
        *,
        group_id: int | None = None,
        user_id: int | None = None,
    ) -> Any:
        """发送消息；不带目标时回到当前会话。"""
        segments = as_segments(message)
        if group_id is not None:
            return await self.connection.send_group_msg(int(group_id), segments)
        if user_id is not None:
            return await self.connection.send_private_msg(int(user_id), segments)
        if self.is_group and self.group_id is not None:
            return await self.connection.send_group_msg(int(self.group_id), segments)
        if self.is_private and self.user_id:
            return await self.connection.send_private_msg(self.user_id, segments)
        raise ValueError("无法推断发送目标：既不是群聊也不是私聊")

    async def reply(
        self,
        message: str | Sequence[MessageSegment] | None,
        *,
        quote: bool = False,
        at_sender: bool = False,
    ) -> Any:
        """回复当前会话，可选引用消息或 @ 发送者。"""
        segments = as_segments(message)
        prefix: list[MessageSegment] = []
        if quote and self.message_id:
            prefix.append(MessageSegment.reply(self.message_id))
        if at_sender and self.is_group and self.user_id:
            prefix.append(MessageSegment.at(self.user_id))
            if segments and segments[0].is_text and not segments[0].text_content.startswith(" "):
                segments[0] = MessageSegment.text(" " + segments[0].text_content)
            prefix.append(MessageSegment.text(" "))
        return await self.send([*prefix, *segments])

    async def finish(
        self,
        message: str | Sequence[MessageSegment] | None = None,
        *,
        quote: bool = False,
        at_sender: bool = False,
    ) -> None:
        """回复并终止后续处理器。"""
        if message is not None:
            await self.reply(message, quote=quote, at_sender=at_sender)
        self.stop()

    def stop(self) -> None:
        """中断当前事件后续处理器。"""
        self.stopped = True

    async def delete(self) -> Any:
        """撤回触发本次处理的消息。"""
        if not self.message_id:
            raise ValueError("当前事件没有消息 ID")
        return await self.connection.delete_msg(self.message_id)

    async def get_member(self, user_id: int | None = None) -> Any:
        """获取群成员信息。"""
        if not self.is_group or self.group_id is None:
            raise ValueError("只有群消息才能获取群成员信息")
        target = user_id or self.user_id
        return await self.connection.get_group_member_info(int(self.group_id), int(target))

    async def get_members(self, *, no_cache: bool = False) -> Any:
        """获取当前群的完整成员列表。"""
        if not self.is_group or self.group_id is None:
            raise ValueError("只有群会话才能获取群成员列表")
        return await self.connection.get_group_member_list(self.group_id, no_cache=no_cache)

    async def poke(self, user_id: int | None = None) -> Any:
        """戳指定用户；默认戳当前事件发送者，自动选择群聊或私聊。"""
        target = self.user_id if user_id is None else user_id
        if not target:
            raise ValueError("戳一戳需要目标用户 ID")
        return await self.connection.send_poke(target, group_id=self.group_id if self.is_group else None)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


__all__ = ["Context"]
