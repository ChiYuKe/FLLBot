"""测试辅助：构造未启动/已启动的机器人、假连接与事件。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fllbot.app import FLLBot
from fllbot.config import ConfigStore, deep_merge, load_config_dict
from fllbot.onebot.segments import MessageSegment

# --------------------------------------------------------------------------- #
# 机器人
# --------------------------------------------------------------------------- #


def build_bot(
    root: Path,
    *,
    builtin: bool = True,
    plugin_dirs: list[Path] | None = None,
    llm: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> FLLBot:
    """构造一个尚未启动的机器人实例（不触碰事件循环）。"""
    root.mkdir(parents=True, exist_ok=True)
    base = load_config_dict(None, root=root, environ={})
    store = ConfigStore(base)

    patch: dict[str, Any] = {
        "server": {"access_token": "test-token", "heartbeat_timeout": 0},
        "admin": {"token": "admin-token"},
        "storage": {"path": str(root / "data" / "fllbot.db")},
        "logging": {"dir": str(root / "logs"), "level": "WARNING", "json_lines": False},
        "plugin": {
            "dirs": [str(item) for item in (plugin_dirs or [root / "plugins"])],
            "builtin": builtin,
            "hot_reload": False,
            "watch_interval": 0.5,
        },
        "bot": {"superusers": [99999]},
    }
    if llm:
        patch["llm"] = llm
    if overrides:
        patch = deep_merge(patch, overrides)
    store.patch(patch)
    return FLLBot(store, root)


async def make_bot(root: Path, **kwargs: Any) -> FLLBot:
    bot = build_bot(root, **kwargs)
    await bot.start()
    return bot


# --------------------------------------------------------------------------- #
# 假连接
# --------------------------------------------------------------------------- #


class FakeConnection:
    """替代真实 OneBotConnection，记录发送内容。"""

    def __init__(self, self_id: int = 10001) -> None:
        self.self_id = self_id
        self.client_role = ""
        self.client_name = ""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sent: list[tuple[str, int, list[MessageSegment]]] = []
        self._message_id = 1000
        self._closed = False

    @property
    def online(self) -> bool:
        return not self._closed

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def label(self) -> str:
        return str(self.self_id)

    def summary(self) -> dict[str, Any]:
        return {"self_id": self.self_id, "online": self.online}

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self._closed = True

    async def send_group_msg(self, group_id: int, message: Any, **extra: Any) -> dict[str, Any]:
        segments = _as_list(message)
        self.calls.append(("send_group_msg", {"group_id": group_id, "message": segments, **extra}))
        self.sent.append(("group", int(group_id), segments))
        self._message_id += 1
        return {"message_id": self._message_id}

    async def send_private_msg(self, user_id: int, message: Any, **extra: Any) -> dict[str, Any]:
        segments = _as_list(message)
        self.calls.append(("send_private_msg", {"user_id": user_id, "message": segments, **extra}))
        self.sent.append(("private", int(user_id), segments))
        self._message_id += 1
        return {"message_id": self._message_id}

    async def send_msg(
        self, message: Any, *, group_id: int | None = None, user_id: int | None = None, **extra: Any
    ) -> dict[str, Any]:
        if group_id:
            return await self.send_group_msg(group_id, message, **extra)
        if user_id:
            return await self.send_private_msg(user_id, message, **extra)
        raise ValueError("缺少目标")

    async def delete_msg(self, message_id: int) -> dict[str, Any]:
        self.calls.append(("delete_msg", {"message_id": message_id}))
        return {}

    async def call(self, action: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        self.calls.append((action, params or {}))
        return {"message_id": 1}

    async def get_group_member_info(self, group_id: int, user_id: int, **_: Any) -> dict[str, Any]:
        return {"group_id": group_id, "user_id": user_id, "nickname": "测试成员", "role": "member"}

    async def set_friend_add_request(self, flag: str, approve: bool = True, remark: str = "") -> dict[str, Any]:
        self.calls.append(("set_friend_add_request", {"flag": flag, "approve": approve, "remark": remark}))
        return {}

    async def set_group_add_request(
        self, flag: str, sub_type: str, approve: bool = True, reason: str = ""
    ) -> dict[str, Any]:
        self.calls.append(
            ("set_group_add_request", {"flag": flag, "sub_type": sub_type, "approve": approve, "reason": reason})
        )
        return {}

    async def set_group_ban(self, group_id: int, user_id: int, duration: int = 600) -> dict[str, Any]:
        self.calls.append(
            ("set_group_ban", {"group_id": group_id, "user_id": user_id, "duration": duration})
        )
        return {}

    # -- 断言工具 --------------------------------------------------------- #
    def texts(self) -> list[str]:
        return [
            "".join(seg.data.get("text", "") for seg in segments if seg.type == "text")
            for _, _, segments in self.sent
        ]

    def actions(self) -> list[str]:
        return [name for name, _ in self.calls]

    def last_text(self) -> str:
        texts = self.texts()
        return texts[-1] if texts else ""


def _as_list(message: Any) -> list[MessageSegment]:
    if isinstance(message, str):
        return [MessageSegment.text(message)]
    if isinstance(message, MessageSegment):
        return [message]
    return [seg if isinstance(seg, MessageSegment) else MessageSegment.model_validate(seg) for seg in message]


# --------------------------------------------------------------------------- #
# 事件构造
# --------------------------------------------------------------------------- #


def group_message(
    text: str,
    *,
    self_id: int = 10001,
    group_id: int = 20002,
    user_id: int = 30003,
    message_id: int = 1,
    at: int | None = None,
    role: str = "member",
    nickname: str = "测试用户",
    message: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造一条 NapCat 风格的群消息上报。"""
    segments = message
    if segments is None:
        segments = []
        if at is not None:
            segments.append({"type": "at", "data": {"qq": str(at)}})
        segments.append({"type": "text", "data": {"text": text}})
    return {
        "time": int(time.time()),
        "self_id": self_id,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "group_id": group_id,
        "user_id": user_id,
        "raw_message": text,
        "font": 0,
        "message": segments,
        "sender": {"user_id": user_id, "nickname": nickname, "card": "", "role": role},
    }


def private_message(
    text: str,
    *,
    self_id: int = 10001,
    user_id: int = 30003,
    message_id: int = 2,
) -> dict[str, Any]:
    return {
        "time": int(time.time()),
        "self_id": self_id,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": user_id,
        "raw_message": text,
        "font": 0,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": user_id, "nickname": "私聊用户", "card": "", "role": "member"},
    }


def heartbeat(self_id: int = 10001) -> dict[str, Any]:
    return {
        "time": int(time.time()),
        "self_id": self_id,
        "post_type": "meta_event",
        "meta_event_type": "heartbeat",
        "interval": 5000,
        "status": {"online": True, "good": True},
    }


def lifecycle(self_id: int = 10001) -> dict[str, Any]:
    return {
        "time": int(time.time()),
        "self_id": self_id,
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": "connect",
    }
