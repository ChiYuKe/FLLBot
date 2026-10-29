"""复读插件。"""

from __future__ import annotations

from fllbot.plugin import Context, Plugin

plugin = Plugin(name="echo", version="0.1.0", description="把你说的话原样复读一遍")


@plugin.on_command("echo", aliases=("复读", "repeat"))
async def echo(ctx: Context) -> None:
    text = ctx.args or ctx.readable
    if not text:
        await ctx.reply("用法：/echo 任意内容")
        return
    await ctx.reply(text)
