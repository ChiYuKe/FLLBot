"""处理器上下文：把事件、连接和机器人能力包在一起递给插件。"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..conversation import ReceiveSnapshot, current_receive
from ..onebot.connection import confirmed_message_id
from ..onebot.models import Event, MessageEvent, Sender
from ..onebot.segments import (
    MessageSegment,
    as_segments,
    plain_text,
    readable_text,
    strip_leading_at_self,
)
from ..tracing import record, span

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
    cancellation_reason: str = ""
    execution_cancelled: bool = False
    execution_parent: Any = field(default=None, repr=False)
    execution_guard: Any = field(default=None, repr=False)
    entrypoint: str = "qq"
    turn_id: str = field(default="", init=False)
    trigger_decision: Any = field(default=None, repr=False)
    reply_settings: Any = field(default=None, repr=False)
    reply_default_chunk_size: int = 0
    context_snapshot: Any = field(default=None, repr=False)
    turn_input: str | None = None
    context_input: str | None = None
    forward_loaded: bool = False
    forward_text: str = ''
    forward_metadata: dict = field(default_factory=dict,repr=False)
    input_projected: bool = False
    turn_reason: str = ""
    turn_silent_reason: str = ""
    tool_call_id: str = ""
    tool_execution_id: str = ""
    memory_summary_before: int = 0
    memory_message_id: int = 0
    lifecycle_epoch: int = field(default=0, init=False)
    data_epoch: int = field(default=0, init=False)
    received: ReceiveSnapshot = field(init=False)
    # regex 规则命中的 Match 对象
    regex_match: re.Match[str] | None = None
    # 被引用消息的解析结果：由 ChatService 在构建请求时填充，同一事件只读取一次。
    quoted_message: Any = field(default=None, repr=False)
    quoted_loaded: bool = field(default=False, repr=False)
    _text_cache: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.event, MessageEvent) and self.event.message_id:
            identity = f'qq:{self.self_id}:{self.event.scene}:{self.event.scene_id}:{self.event.message_id}'
            self.turn_id = uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        else:
            self.turn_id = uuid.uuid4().hex
        self.lifecycle_epoch = getattr(self.bot,'lifecycle_epoch',0)
        generation = getattr(getattr(self.bot, 'storage', None), 'conversation_generation', 0)
        self.received = current_receive() or ReceiveSnapshot.capture(generation, getattr(getattr(self.bot, "config", None), "plugin", None),
            self.bot.bots.epoch(self.self_id) if hasattr(self.bot, "bots") else 0,
            scope_generation=self.bot.storage.scope_generation(self.self_id,self.event.scene,self.event.scene_id)
            if hasattr(self.bot,'storage') and isinstance(self.event,MessageEvent) else 0)
        if self.received.turn_id:
            self.turn_id = self.received.turn_id
        self.data_epoch = self.received.data_epoch
        chat = getattr(self.bot,'chat',None)
        if self.received.memory_allowed is None and chat is not None and isinstance(self.event,MessageEvent):
            self.received = replace(self.received,memory_allowed=chat.memory.eligible(self))

    @property
    def data_current(self) -> bool:
        accounts = getattr(self.bot, 'bots', None)
        account_current = accounts is None or (accounts.enabled(self.self_id) and accounts.epoch(self.self_id) == self.received.account_epoch)
        storage = getattr(self.bot,'storage',None)
        scope_current = storage is None or not isinstance(self.event,MessageEvent) or storage.scope_current(
            self.self_id,self.event.scene,self.event.scene_id,self.received.scope_generation)
        return (not self.execution_cancelled
                and (self.entrypoint != 'qq' or int(getattr(self.connection,'self_id',0) or 0) == self.self_id)
                and (self.execution_parent is None or self.execution_parent.data_current)
                and (self.execution_guard is None or self.execution_guard())
                and not getattr(self.bot,'stopping',False)
                and self.lifecycle_epoch == getattr(self.bot,'lifecycle_epoch',0)
                and not self.cancellation_reason and account_current and scope_current
                and self.data_epoch == getattr(getattr(self.bot, 'storage', None), 'conversation_generation', 0))

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
    def reply_id(self) -> int:
        """当前消息引用的原消息 ID；没有引用或编号无效时为 0。"""
        for segment in self.message:
            if segment.type != "reply":
                continue
            try:
                return int(str(segment.get("id") or "").strip() or 0)
            except (TypeError, ValueError):
                return 0
        return 0

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
    def targeted_segments(self, message, *, group_id=None, user_id=None):
        """Attribute every group-task message, including chunks and tool media."""
        segments = as_segments(message)
        if (self.entrypoint != 'qq' or not self.is_group or self.turn_input is None
                or user_id is not None or group_id is not None and int(group_id) != self.group_id):
            return segments
        if any(segment.type == 'reply' for segment in segments):
            return segments
        if self.message_id:
            return [MessageSegment.reply(self.message_id), *segments]
        if self.user_id and not any(segment.type == 'at' and str(segment.get('qq')) == str(self.user_id) for segment in segments):
            return [MessageSegment.at(self.user_id), MessageSegment.text(' '), *segments]
        return segments

    def trace_event(self, stage: str, code: str, status: str = 'passed', component: str = '') -> None:
        """Record metadata-only diagnostics; never pass message content or arguments here."""
        record(stage, code, status, component)

    def trace_span(self, stage: str, component: str = '') -> Any:
        return span(stage, component)

    async def send(
        self,
        message: str | Sequence[MessageSegment] | None,
        *,
        group_id: int | None = None,
        user_id: int | None = None,
    ) -> Any:
        """发送消息；不带目标时回到当前会话。"""
        if not self.data_current:
            self.trace_event('trigger', 'context_cleared', 'skipped')
            return None
        with span('send'):
            segments = self.targeted_segments(message, group_id=group_id, user_id=user_id)
            if group_id is not None:
                receipt = await self.connection.send_group_msg(int(group_id), segments)
            elif user_id is not None:
                receipt = await self.connection.send_private_msg(int(user_id), segments)
            elif self.is_group and self.group_id is not None:
                receipt = await self.connection.send_group_msg(int(self.group_id), segments)
            elif self.is_private and self.user_id:
                receipt = await self.connection.send_private_msg(self.user_id, segments)
            else:
                raise ValueError("无法推断发送目标：既不是群聊也不是私聊")
            confirmed_message_id(receipt)
            return receipt

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
