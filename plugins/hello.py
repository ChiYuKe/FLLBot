"""示例插件：改动保存后会自动热重载。"""

from fllbot.plugin import Context, Plugin

plugin = Plugin(name="hello", version="0.1.0", description="示例插件：/hello 打个招呼")


@plugin.on_command("hello", aliases=("你好",))
async def hello(ctx: Context) -> None:
    await ctx.reply(f"你好，{ctx.nickname}！我是 FLLBot。", quote=True)
