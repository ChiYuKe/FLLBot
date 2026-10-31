"""权限、群开关与限流。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .app import FLLBot
    from .plugin.context import Context


class PermissionManager:
    """集中判断“谁能用什么功能”，并把结果写进数据库。"""

    def __init__(self, bot: FLLBot) -> None:
        self.bot = bot
        self.logger = logging.getLogger("fllbot.permission")
        # group_id -> {"enabled": bool, "config": dict}
        self._groups: dict[int, dict[str, Any]] = {}
        # (plugin, group_id) -> bool，group_id=0 表示全局
        self._plugin_state: dict[tuple[str, int], bool] = {}
        # (plugin, handler, user) -> 上次触发时间
        self._cooldowns: dict[tuple[str, str, int], float] = {}
        # 首次登记群时防止并发重复插入
        self._group_lock = asyncio.Lock()

    # -- 装载 ------------------------------------------------------------- #
    async def load(self) -> None:
        self._groups = await self.bot.storage.list_group_states()
        self._plugin_state = await self.bot.storage.list_plugin_states()
        self.logger.debug("权限数据已载入：%d 个群，%d 条插件状态", len(self._groups), len(self._plugin_state))

    # -- 超管 ------------------------------------------------------------- #
    def is_superuser(self, user_id: int | None) -> bool:
        if not user_id:
            return False
        return int(user_id) in set(self.bot.config.bot.superusers)

    def is_group_admin(self, ctx: Context) -> bool:
        if self.is_superuser(ctx.user_id):
            return True
        if not self.bot.config.permission.group_admin_permission:
            return False
        return ctx.sender.role in {"owner", "admin"}

    # -- 群开关 ----------------------------------------------------------- #
    @property
    def allow_private(self) -> bool:
        return bool(self.bot.config.permission.allow_private)

    def group_enabled(self, group_id: int | None) -> bool:
        if group_id is None:
            return self.allow_private
        gid = int(group_id)
        permission = self.bot.config.permission
        if gid in set(permission.deny_groups):
            return False
        restricted = permission.group_access == "listed" or permission.group_access == "auto" and bool(permission.allow_groups)
        if restricted and gid not in set(permission.allow_groups):
            return False
        state = self._groups.get(gid)
        if state is None:
            return bool(permission.default_group_enabled)
        if (state.get("config") or {}).get("_inherit_enabled"):
            return bool(permission.default_group_enabled)
        return bool(state.get("enabled", True))

    def group_config(self, group_id: int) -> dict[str, Any]:
        state = self._groups.get(int(group_id)) or {}
        config = state.get("config") or {}
        return dict(config) if isinstance(config, dict) else {}

    async def ensure_group(self, group_id: int) -> dict[str, Any]:
        """登记一个群（首次见到时自动入库）。"""
        gid = int(group_id)
        state = self._groups.get(gid)
        if state is not None:
            return state
        async with self._group_lock:
            state = self._groups.get(gid)
            if state is not None:
                return state
            state = {
                "enabled": bool(self.bot.config.permission.default_group_enabled),
                "config": {},
            }
            await self.bot.storage.upsert_group(gid, enabled=state["enabled"], config={})
            self._groups[gid] = state
            return state

    async def set_group_enabled(self, group_id: int, enabled: bool) -> None:
        gid = int(group_id)
        await self.ensure_group(gid)
        config = self.group_config(gid)
        config.pop("_inherit_enabled", None)
        await self.save_group(gid, enabled=enabled, config=config)

    async def set_group_config(self, group_id: int, config: dict[str, Any]) -> None:
        gid = int(group_id)
        await self.ensure_group(gid)
        await self.bot.storage.upsert_group(gid, enabled=None, config=config)
        self._groups.setdefault(gid, {})["config"] = dict(config)

    async def save_group(self, group_id: int, *, enabled: bool, config: dict[str, Any], reset_agent: bool = False) -> None:
        """将一组单群设置作为一次数据库更新保存。"""
        gid = int(group_id)
        async with self._group_lock:
            agent = self.bot.plugins.get("agent") if reset_agent else None
            await self.bot.storage.upsert_group(gid, enabled=enabled, config=config,
                                                clear_settings_scope=agent.settings.scope if agent else None)
            if agent:
                agent.settings.forget_group(gid)
            self._groups[gid] = {"enabled": enabled, "config": dict(config)}

    def list_groups(self) -> list[dict[str, Any]]:
        result = []
        for gid, state in sorted(self._groups.items()):
            result.append(
                {
                    "group_id": gid,
                    "enabled": bool(state.get("enabled", True)),
                    "config": state.get("config") or {},
                }
            )
        return result

    # -- 插件开关 --------------------------------------------------------- #
    def plugin_enabled(self, plugin_name: str, group_id: int | None) -> bool:
        if group_id is not None:
            override = self._plugin_state.get((plugin_name, int(group_id)))
            if override is not None:
                return override
        global_state = self._plugin_state.get((plugin_name, 0))
        if global_state is not None:
            return global_state
        plugin = self.bot.plugins.get(plugin_name)
        return bool(plugin.enabled_by_default) if plugin is not None else True

    async def set_plugin_enabled(self, plugin_name: str, enabled: bool, group_id: int | None = None) -> None:
        gid = int(group_id or 0)
        await self.bot.storage.set_plugin_state(plugin_name, gid, enabled)
        self._plugin_state[(plugin_name, gid)] = bool(enabled)

    def plugin_state(self, plugin_name: str, group_id: int | None = None) -> bool | None:
        return self._plugin_state.get((plugin_name, int(group_id or 0)))

    # -- 权限判定 --------------------------------------------------------- #
    def check(self, requirement: str | None, ctx: Context) -> bool:
        """``requirement`` 可为 ``superuser`` / ``group_admin`` / ``group`` / ``private``。

        可用 ``|`` 组合表示“满足其一”，例如 ``"superuser|group_admin"``。
        """
        if not requirement:
            return True
        for part in str(requirement).split("|"):
            if self._check_one(part.strip().lower(), ctx):
                return True
        return False

    def _check_one(self, requirement: str, ctx: Context) -> bool:
        if requirement in ("", "any", "all"):
            return True
        if requirement == "superuser":
            return self.is_superuser(ctx.user_id)
        if requirement in ("group_admin", "admin"):
            return ctx.is_group and self.is_group_admin(ctx)
        if requirement == "group":
            return ctx.is_group
        if requirement == "private":
            return ctx.is_private
        if requirement == "owner":
            return ctx.sender.role == "owner"
        self.logger.warning("未知权限要求：%s", requirement)
        return False

    # -- 限流 ------------------------------------------------------------- #
    def cooldown_ok(self, key: tuple[str, str, int], seconds: float) -> bool:
        if seconds <= 0:
            return True
        now = time.monotonic()
        last = self._cooldowns.get(key, 0.0)
        if now - last < seconds:
            return False
        self._cooldowns[key] = now
        self._cleanup_cooldowns(now)
        return True

    def cooldown_remaining(self, key: tuple[str, str, int], seconds: float) -> float:
        if seconds <= 0:
            return 0.0
        last = self._cooldowns.get(key, 0.0)
        return max(0.0, seconds - (time.monotonic() - last))

    def _cleanup_cooldowns(self, now: float) -> None:
        if len(self._cooldowns) < 5000:
            return
        expired = [key for key, ts in self._cooldowns.items() if now - ts > 600]
        for key in expired:
            self._cooldowns.pop(key, None)


__all__ = ["PermissionManager"]
