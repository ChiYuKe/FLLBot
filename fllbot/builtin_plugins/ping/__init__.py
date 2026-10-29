"""连通性测试插件。"""

from __future__ import annotations

from fllbot.plugin import Context, Plugin

plugin = Plugin(name="ping", version="0.1.0", description="连通性测试：确认机器人是否在线")


@plugin.on_command("ping", aliases=("在吗", "pong"))
async def ping(ctx: Context) -> None:
    await ctx.reply("pong", quote=True)
