# 用户插件目录

把 `*.py` 文件或 `<名字>/__init__.py` 包放在这里，启动时会自动加载，文件改动会触发热重载
（由 `config.toml` 的 `plugin.hot_reload` 控制）。

最小插件：

```python
from fllbot.plugin import Context, Plugin

plugin = Plugin(name="demo", description="示例")


@plugin.on_command("demo")
async def demo(ctx: Context) -> None:
    await ctx.reply("hello")
```

注意：

- 模块里必须有 `plugin = Plugin(...)`，`name` 全局唯一
- 文件名/目录名以 `_` 或 `.` 开头的会被跳过
- 用 `bot.plugins.failed` 或 `GET /api/plugins` 查看加载失败原因，日志前缀 `fllbot.plugin.loader`

完整文档见 [../docs/plugin-api.md](../docs/plugin-api.md)。
