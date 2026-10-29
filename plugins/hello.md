# hello（示例插件）

最小可运行的用户插件示例：收到 `/hello` 时回复一句问候。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/hello` | 回复问候 |
| `/你好` | 中文别名 |

## 代码

```python
from fllbot.plugin import Context, Plugin

plugin = Plugin(name="hello", version="0.1.0", description="示例插件：/hello 打个招呼")


@plugin.on_command("hello", aliases=("你好",))
async def hello(ctx: Context) -> None:
    await ctx.reply(f"你好，{ctx.sender_name or ctx.user_id}！")
```

## 说明

- 用户插件放在 `plugins/` 目录，单文件插件是 `plugins/<name>.py`
- 这个插件的说明来自同目录的 `plugins/hello.md`
- 目录插件写成 `plugins/<name>/__init__.py`（里面定义 `plugin`），说明放在 `plugins/<name>/README.md`
- 保存后热重载会重新加载，日志里能看到「已加载插件 hello」
