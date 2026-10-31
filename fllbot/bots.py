"""账号身份缓存与每个 Bot 的默认行为，群覆盖始终优先。"""
from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BotDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply_mode: Literal["inherit", "at", "prefix", "all", "off"] = "inherit"
    wake_word: str = Field(default="", max_length=80)
    provider: str = Field(default="", max_length=80)
    persona: str = Field(default="", max_length=80)

    @field_validator("wake_word")
    @classmethod
    def normalize_wake_word(cls, value: str) -> str:
        return value.strip()


class BotProfiles:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.items: dict[str, dict[str, Any]] = {}
        self.lock = asyncio.Lock()

    async def load(self) -> None:
        self.items = await self.bot.storage.kv_all("bot-profiles")

    def get(self, self_id: int | None) -> dict[str, Any]:
        return dict(self.items.get(str(self_id), {}))

    async def update(self, self_id: int, **fields: Any) -> None:
        async with self.lock:
            value = {**self.get(self_id), **fields}
            await self.bot.storage.kv_set("bot-profiles", str(self_id), value)
            self.items[str(self_id)] = value

    def nickname(self, self_id: int) -> str:
        if not self_id:
            return ""
        conn = self.bot.manager.by_self_id(self_id)
        return str(getattr(conn, "nickname", "") or self.get(self_id).get("nickname", "")).strip()

    def defaults(self, self_id: int | None) -> BotDefaults:
        return BotDefaults.model_validate(self.get(self_id).get("defaults", {}))

    def wake_word(self, self_id: int | None, nickname: str = "") -> str:
        return self.defaults(self_id).wake_word or nickname.strip() or self.nickname(self_id)

    async def sync(self, conn: Any) -> None:
        data = await asyncio.wait_for(conn.get_login_info(), timeout=8)
        if not isinstance(data, dict):
            raise ValueError("账号未返回昵称")
        user_id = int(data.get("user_id") or conn.self_id or 0)
        if not user_id or (conn.self_id and user_id != conn.self_id):
            raise ValueError("账号身份不匹配")
        nickname = str(data.get("nickname") or "").strip()
        if not nickname:
            raise ValueError("账号未返回昵称")
        conn.self_id, conn.nickname = user_id, nickname
        await self.update(user_id, nickname=nickname)

    def summaries(self) -> list[dict[str, Any]]:
        live = {str(conn.self_id): conn for conn in self.bot.manager.list() if conn.self_id}
        ids = set(self.items) | set(live)
        return [{
            "self_id": int(key), "nickname": self.nickname(int(key)),
            "online": bool(key in live and live[key].online),
            "defaults": self.defaults(int(key)).model_dump(),
        } for key in sorted(ids, key=int)]
