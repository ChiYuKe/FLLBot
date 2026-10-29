# 插件开发

插件是放在 `plugins/` 目录下的 Python 文件（`xxx.py`）或包（`xxx/__init__.py`），
模块级必须导出 `plugin = Plugin(...)`。文件改动保存后自动热重载（`plugin.hot_reload = true`）。

## 最小示例

```python
from fllbot.plugin import Context, Plugin

plugin = Plugin(name="demo", version="0.1.0", description="演示")


@plugin.on_command("demo")
async def demo(ctx: Context) -> None:
    await ctx.reply("hello")
```

也可以子类化：

```python
class DemoPlugin(Plugin):
    name = "demo"
    description = "演示"

plugin = DemoPlugin()
```

## Plugin

| 属性 | 说明 |
| --- | --- |
| `name` | 唯一标识，同时用于插件开关与数据库记录 |
| `version` | 版本号，展示用 |
| `description` | 说明，`/help`、`/plugins` 和管理 API 会展示 |
| `author` | 作者（可选） |
| `enabled_by_default` | 首次加载时默认是否启用（默认 `True`） |

生命周期钩子：

```python
@plugin.on_startup
async def setup(bot) -> None:      # 回调参数是 FLLBot 实例
    ...

@plugin.on_shutdown
async def teardown(bot) -> None:
    ...
```

插件类上也可以重写 `async def on_load(self, bot)` / `on_unload(self, bot)`。

## 装饰器

### on_command

```python
@plugin.on_command(
    "card",                 # 主命令名
    aliases=("名片",),      # 别名
    prefix=None,            # 覆盖全局前缀；None 用 config.bot.command_prefix
    priority=10,            # 越小越先执行
    block=True,             # 命中后阻断后续处理器（命令默认 True）
    permission=None,        # "superuser" / "group_admin" / "group" / "private"，可 "a|b"
    cooldown=0.0,           # 同一用户冷却秒数
    only_to_me=False,       # 只处理私聊或 @ 机器人的消息
    ignore_case=True,
)
```

命中后 `ctx.command` 是规范化后的命令名，`ctx.args` 是整段参数，`ctx.argv` 是切分后的列表。

识别三种写法：`/card xxx`（配置前缀）、`@机器人 card xxx`、
以及 `command_prefix` 中加 `""` 后的裸命令（或 `bot.allow_bare_command = true` 且被 @）。

### on_message

```python
@plugin.on_message(rule=keyword("早"), priority=80, block=True)
async def morning(ctx: Context) -> None:
    await ctx.reply("早上好")
```

`rule` 可以是同步或异步函数（参见「规则」）。不写 `rule` 表示匹配所有消息。

### on_notice / on_request / on_meta_event

```python
@plugin.on_notice("group_increase", "group_decrease")
async def member_change(ctx: Context) -> None:
    ...

@plugin.on_request("friend", "group")
async def on_request(ctx: Context) -> None:
    ...

@plugin.on_meta_event("heartbeat")
async def on_beat(ctx: Context) -> None:
    ...
```

事件类型的字段与 OneBot 11 一致：`ctx.event.notice_type`、`ctx.event.request_type`、
`ctx.event.flag`、`ctx.event.sub_type` 等；未建模的字段也能通过 `ctx.event.get("xxx")` 读到。

### on_loop

```python
@plugin.on_loop(60, name="cleanup")
async def cleanup(bot) -> None:
    ...     # 每 60 秒执行一次，回调参数是 FLLBot 实例
```

卸载插件时会自动取消任务。

## 声明式配置

插件可以声明自己的配置项，管理端（含你的桌面端）能用 `/api/plugins/{name}/settings`
拿到 schema 并直接渲染表单，不需要为每个插件写界面。

```python
plugin = Plugin(name="welcome", description="入群欢迎")

plugin.setting("enabled", type="bool", label="启用欢迎", default=True, group_scoped=True)
plugin.setting("text", type="text", label="欢迎语", default="欢迎 {nickname} 加入～", group_scoped=True)
plugin.setting("max_len", type="int", label="最长字数", default=100, min=10, max=1000)
plugin.setting("mode", type="select", label="模式", default="all", options=["all", "admin"])
plugin.setting("keywords", type="list", label="关键词", default=[])
```

字段参数：

| 参数 | 说明 |
| --- | --- |
| `key` | 配置键，读写时使用 |
| `type` | `bool` / `int` / `float` / `string` / `text` / `select` / `multiselect` / `list` |
| `label` | 表单里显示的标题 |
| `default` | 默认值 |
| `description` | 帮助文字 |
| `options` | `select` / `multiselect` 的可选值 |
| `min` / `max` | 数值范围（越界会拒绝写入） |
| `secret` | 标记为敏感值，管理 API 返回值会打码 |
| `group_scoped` | 允许按群覆盖 |

读取（自动应用本群覆盖）：

```python
enabled = ctx.settings.get("enabled")
text = ctx.settings.get("text")
全部 = ctx.settings.all()
```

写入（群级字段写到当前群，其他写全局）：

```python
await ctx.settings.set("enabled", False)
await ctx.settings.set("keywords", ["广告", "re:加\\s*群"])
```

> 写入会做类型校验，非法值抛 `ValueError` 且不落库；批量更新用
> `plugin.settings.update({...}, group_id)`，任一字段非法则整体不生效。

## 插件说明（README）

插件可以带一份 Markdown 说明，管理端通过 `/api/plugins/{name}/readme` 读取并展示：

- 目录插件：`plugins/<name>/README.md`
- 单文件插件：`plugins/<name>.md`
- 代码里直接赋值（优先级最高）：`plugin.readme = "# 标题\n..."`

`/api/plugins` 返回的每个插件带 `has_readme`，管理端据此决定是否显示「说明」入口。

```python
plugin = Plugin(name="demo", version="1.0.0")
plugin.readme = """# demo

用法：`/demo`
"""
```

## Context

| 成员 | 说明 |
| --- | --- |
| `ctx.event` | 强类型事件（`MessageEvent` / `NoticeEvent` / `RequestEvent` / `MetaEvent`） |
| `ctx.connection` | 底层 WS 连接，可直接调用任意 OneBot API |
| `ctx.bot` | FLLBot 实例 |
| `ctx.config` | 当前生效配置（`AppConfig`） |
| `ctx.db` | `Storage`，见下文 |
| `ctx.settings` | 当前插件的配置视图（`get` / `set` / `all`），群级字段自动取本群值 |
| `ctx.llm` | `ChatService` |
| `ctx.self_id` / `ctx.user_id` / `ctx.group_id` / `ctx.message_id` | 常用标识 |
| `ctx.is_group` / `ctx.is_private` / `ctx.to_me` | 场景判断 |
| `ctx.sender` | 发送者信息（`nickname` / `card` / `role`） |
| `ctx.text` | 去掉开头 `@机器人` 的纯文本（命令解析用的就是它） |
| `ctx.raw_text` | 原始纯文本 |
| `ctx.readable` | 带占位符的可读文本（`[图片]`、`@123`） |
| `ctx.message` | 消息段列表（`fllbot.onebot.MessageSegment`） |
| `ctx.command` / `args` / `argv` | 命令解析结果 |
| `ctx.is_superuser` / `ctx.is_group_admin` | 权限快捷判断 |
| `ctx.plugin` / `ctx.handler` | 当前插件与处理器声明 |

发送：

```python
await ctx.reply("文本")                        # 回到当前会话
await ctx.reply("文本", quote=True)            # 引用触发消息
await ctx.reply("文本", at_sender=True)        # 群里 @ 发送者
await ctx.finish("再见")                       # 回复并阻断后续处理器
await ctx.send("广播", group_id=123456)        # 指定目标
await ctx.send("私聊", user_id=10001)
await ctx.delete()                             # 撤回触发消息
await ctx.get_member()                         # 群成员信息
ctx.stop()                                     # 只阻断，不回复
```

消息段：

```python
from fllbot.onebot import MessageSegment

await ctx.reply([MessageSegment.at(123456), MessageSegment.text(" 看这里")])
await ctx.reply([MessageSegment.image("https://example.com/a.png")])
```

## 规则

```python
from fllbot.plugin import (
    all_rules, any_rule, not_rule,
    command, keyword, startswith, endswith, regex, fullmatch,
    to_me, in_group, in_private,
)
```

- `keyword("早", "早安")`：文本包含任一关键词
- `regex(r"^查询\s*(.+)")`：正则搜索，命中后 `ctx.regex_match` 可用
- `startswith("/")` / `endswith("吗")`
- `to_me()` / `in_group(123)` / `in_private()`
- 组合：`all_rules(...)`、`any_rule(...)`、`not_rule(...)`

规则可以是 `async def rule(ctx) -> bool`。

## 优先级与阻断

- 同一条消息的处理器按 `priority` 升序执行（命令默认 10，普通消息默认 20，兜底建议 90+）
- `block=True` 或调用 `ctx.stop()` / `ctx.finish()` 后，后续处理器不再执行
- 处理器抛异常只会记录日志，不会影响其他插件；配置 `bot.error_reply` 可回一句提示
- 单个处理器受 `plugin.handler_timeout` 限制

## 存储

```python
await ctx.db.kv_set("demo", "count", 1)          # 插件私有配置（scope 建议用插件名）
value = await ctx.db.kv_get("demo", "count", default=0)
await ctx.db.kv_set("demo", "obj", {"a": 1})     # 自动 JSON 序列化
await ctx.db.kv_delete("demo", "count")
items = await ctx.db.kv_all("demo")

await ctx.db.add_chat_message("group", 123, "user", "hi")
rows = await ctx.db.recent_chat("group", 123, limit=20)
await ctx.db.clear_chat("group", 123)

await ctx.db.upsert_group(123, enabled=True, config={"llm": {"persona": "concise"}})
groups = await ctx.db.list_group_states()

await ctx.db.daily_message_counts(days=7)

# 定时提醒（reminder 插件用的就是这套）
await ctx.db.add_reminder(scene="group", scene_id=123, creator=456, content="喝水", due_at=ts)
await ctx.db.due_reminders()
await ctx.db.mark_reminder_fired(1)
await ctx.db.list_reminders("group", 123)
await ctx.db.delete_reminder(1, scene="group", scene_id=123)
```

`kv` 也可以从管理 API 读写：`GET /api/kv/demo`、`PUT /api/kv/demo/count`。

## 大模型

```python
@plugin.on_message(rule=keyword("翻译"))
async def translate(ctx: Context) -> None:
    reply = await ctx.llm.test(f"把这句话翻译成英文：{ctx.text}")
    await ctx.reply(reply.text)
```

`ctx.llm`（`ChatService`）：

| 方法 | 说明 |
| --- | --- |
| `available` | 是否已启用且配置了 API Key |
| `respond(ctx, text, persona_name=None)` | 完整的多轮对话：取历史、裁剪、调用、存历史、分条回复；返回 `ChatReply` 或 `None` |
| `test(prompt, persona_name=None)` | 单轮直连，不写历史 |
| `build_messages(scene, scene_id, text, ...)` | 只构造消息列表 |
| `clear(scene, scene_id)` | 清空历史 |
| `persona_name(scene, scene_id)` | 当前生效人格名 |

## 权限

```python
@plugin.on_command("ban", permission="group_admin")
async def ban(ctx: Context) -> None:
    ...
```

可用权限串：

| 值 | 含义 |
| --- | --- |
| `superuser` | `bot.superusers` 里的 QQ |
| `group_admin` | 群主 / 管理员（受 `permission.group_admin_permission` 控制） |
| `group` | 仅群聊 |
| `private` | 仅私聊 |
| `owner` | 仅群主 |

`"superuser|group_admin"` 表示满足其一即可。

## 常用 OneBot API

`ctx.connection` 暴露了常用封装，也可以直接调任意 `action`：

```python
await ctx.connection.send_group_msg(group_id, [MessageSegment.at(uid), MessageSegment.text(" hi")])
await ctx.connection.send_private_msg(user_id, "hi")
await ctx.connection.delete_msg(message_id)
await ctx.connection.get_group_member_info(group_id, user_id)
await ctx.connection.get_group_list()
await ctx.connection.set_group_ban(group_id, user_id, duration=600)
await ctx.connection.set_group_kick(group_id, user_id)
await ctx.connection.set_group_card(group_id, user_id, "新名片")
await ctx.connection.call("get_status")        # 任意接口
```

失败会抛 `fllbot.onebot.connection.ActionError`（含 `retcode` 与 `msg`）。

## 调试

- `bot.plugins.reload("demo")`（或 `POST /api/plugins/demo/reload`）手动重载
- `GET /api/logs?level=ERROR` 查看插件异常
- 日志里带 `fllbot.plugin.<name>` 前缀
- 测试里可以直接构造事件并调用 `await bot.dispatcher.dispatch(event, fake_connection)`，
  参考 `tests/test_dispatcher.py`

## 注意事项

- 插件模块在进程内执行，等同于本地代码权限，只加载可信来源
- 热重载会重新执行源码，模块级全局变量会被重置；持久状态请放 SQLite
- 卸载时务必在 `on_unload` / `on_shutdown` 里取消自己创建的任务与连接
- 群聊默认受群开关控制；被停用的群里不会触发任何处理器
