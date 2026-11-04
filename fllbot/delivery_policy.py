"""Live access checks shared by initial delivery, media and controlled recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .plugin.context import Context


def delivery_allowed(ctx: Context, require_ai: bool = True) -> bool:
    """Recheck mutable access immediately before each protocol operation."""
    if not ctx.data_current:
        return False
    if getattr(ctx.connection, "_closed", False):
        return False
    if int(getattr(ctx.connection, "self_id", 0) or 0) != ctx.self_id:
        return False
    permissions = ctx.bot.permissions
    if ctx.is_group:
        if not permissions.group_enabled(ctx.group_id):
            return False
    elif not permissions.allow_private:
        return False
    if ctx.plugin:
        if not permissions.plugin_enabled(ctx.plugin.name, ctx.group_id):
            return False
        if require_ai and not ctx.bot.chat.scene_enabled(
            "group" if ctx.is_group else "private", ctx.group_id if ctx.is_group else ctx.user_id
        ):
            return False
    if ctx.handler and not permissions.check(ctx.handler.permission, ctx):
        return False
    return True
