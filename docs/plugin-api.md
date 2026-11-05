# 插件开发

新插件统一使用 **一个文件夹一个插件包**，`plugin.toml` 是唯一包头，`__init__.py` 导出 `plugin`，
实现放在 `plugin.py`、`handlers.py`、`settings.py` 等包内模块。包规范、文档与文件头约定见
[插件包开发规范](plugin-packages.md)，宿主职责与依赖方向见 [项目架构](architecture.md)。

```powershell
.venv/Scripts/python.exe -m fllbot plugin new hello --description "打招呼插件" --author "你的名字"
.venv/Scripts/python.exe -m fllbot plugin new helper --template tool --description "示例 AI 工具"
.venv/Scripts/python.exe -m fllbot plugin check --builtin
.venv/Scripts/python.exe -m fllbot plugin list
```

包内使用 `plugin = Plugin.from_manifest(__file__)` 读取元信息，文件变动自动热重载（`plugin.hot_reload = true`）。
旧单文件插件与无清单的目录插件仍能运行，用于平稳迁移；新建模板和内置插件均采用标准包。

生命周期资源使用 `plugin.on_cleanup(callback)`，支持同步或异步回调，卸载、重载及初始化失败时逆序清理。
文件数据使用 `ctx.data_dir` / `plugin.data_dir`，对应宿主 `data/plugins/<name>/`；配置仍由 `plugin.setting()`
声明并保存到宿主数据库，群覆盖与密钥掩码规则保持不变。

## 装饰器最小示例（旧声明写法仍兼容）

插件可用 `@plugin.service_check("tts", "语音服务", timeout=3, selected_when=("backend", "qwen"))`
声明后台健康检查。回调为无参数 async 函数，只做只读、不会触发生成的健康请求，返回
`{"state": "ready", "code": "ready"}`。state 支持 ready/loading/idle/unavailable/error，code 使用固定英文原因码，
不返回异常正文或密钥。每个插件最多 8 个不重名检查，超时不超过 10 秒；回调不能使用阻塞式 I/O。
selected_when 为可选配置字段和值，仅标记当前选用状态。声明不会自动执行；由管理员在“系统设置 → 外部服务”按需调用。

消息处理期间可以使用 `ctx.trace_event(stage, code, status="passed", component="")` 记录固定原因码，
或使用 `with ctx.trace_span(stage, component=""):` 包住一次异步操作，自动记录耗时与成功/失败。
仅传入固定原因码和插件/工具标识，不能传入消息、提示词、参数、结果或密钥。没有活动消息链路时这些调用不记录数据，
不会改变插件控制流；插件脱离当前消息启动的后台任务不写入已结束的消息记录。宿主已自动记录常规处理器、模型和发送阶段。

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

### on_ai_tool

智能体只会看到群策略「允许调用的工具」名单中的工具。插件是安装和配置的单位，工具是插件显式开放给模型的能力；未登记的处理器、OneBot API 和其他插件能力不会暴露给模型。工具执行时会再次检查插件开关和权限；工具回调也应校验自己的业务权限与数据范围。

```python
@plugin.on_ai_tool(
    "lookup_order",
    title="订单查询",
    description="按订单号查询当前用户的订单状态",
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "订单编号"}},
        "required": ["order_id"],
        "additionalProperties": False,
    },
    permission="group",
)
async def lookup_order(ctx: Context, order_id: str) -> dict[str, str]:
    # 在这里执行插件自己的权限和数据范围检查。
    return {"order_id": order_id, "status": "处理中"}
```

`title` 是管理端展示用的名称（例如「联网搜索」），省略时回退到工具名。回调签名为 `async def callback(ctx, **arguments)`，工具 ID 会以 `插件名__工具名` 的形式交给模型，也是单群策略 `allowed_tools` 里保存的值。模型服务需兼容 OpenAI Chat Completions 的 `tools` / `tool_calls` 格式。

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

await ctx.db.add_chat_message(
    "group", 123, "user", "hi", user_id=456, nickname="小明", sender_role="member",
)
rows = await ctx.db.recent_chat("group", 123, limit=20)
# rows 为 ChatRecord：.role / .content / .user_id / .nickname / .sender_role
await ctx.db.clear_chat("group", 123)

await ctx.db.upsert_group(123, enabled=True, config={"llm": {"persona": "concise"}})
groups = await ctx.db.list_group_states()

await ctx.db.daily_message_counts(days=7)

# 定时提醒数据存储接口
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
- 热重载只监听 `plugins/` 目录（含包内 `*.py`）；`fllbot/builtin_plugins/` 的改动需要重启进程
- 重载会先导入新源码、成功后替换旧版本：新版本有语法/导入错误时重载失败，旧插件继续运行；
  修好源码再保存，下一次扫描会自动重载成功
- 卸载时务必在 `on_unload` / `on_shutdown` 里取消自己创建的任务与连接
- 群聊默认受群开关控制；被停用的群里不会触发任何处理器


## QQ 交互能力补充

新增接口对照 [NapCat 官方 API](https://napneko.github.io/onebot/api)。所有方法均为异步，
返回 NapCat 的 `data`；失败抛 `ActionError`，不会自动重试写操作。实际权限和支持情况由 NapCat/QQ 决定。
这些接口供插件使用，不会自动注册聊天命令或向智能体开放工具。

| 类别 | `ctx.connection` 方法 |
| --- | --- |
| 用户与成员 | `get_group_member_list(group_id, no_cache=False)`、`get_stranger_info(user_id, no_cache=False)` |
| 戳一戳与点赞 | `group_poke(group_id, user_id)`、`friend_poke(user_id)`、`send_poke(user_id, group_id=None)`、`send_like(user_id, times=1)` |
| 群管理 | `set_group_admin`、`set_group_name`、`set_group_leave`、`set_group_special_title`、`set_group_portrait` |
| 公告 | `send_group_notice(group_id, content, image=None)`、`get_group_notice(group_id)`、`delete_group_notice(group_id, notice_id)` |
| 精华与荣誉 | `get_essence_msg_list`、`set_essence_msg`、`delete_essence_msg`、`get_group_honor_info`、`get_group_at_all_remain` |
| 文件上传 | `upload_group_file(group_id, file, name, folder=None)`、`upload_private_file(user_id, file, name)` |
| 群文件浏览 | `get_group_file_system_info`、`get_group_root_files`、`get_group_files_by_folder(group_id, folder_id)`、`get_group_file_url(group_id, file_id, busid)` |
| 群文件管理 | `delete_group_file`、`create_group_file_folder`、`delete_group_folder`、`move_group_file`、`trans_group_file`、`rename_group_file` |
| 媒体与转发 | `get_image(file)`、`get_record(file, out_format="mp3")`、`can_send_image()`、`can_send_record()`、`get_forward_msg(message_id)` |
| 好友备注 | `set_friend_remark(user_id, remark)` |

表中的可选参数如 `no_cache`、`group_id`、`image`、`folder` 使用关键字传递。
公告方法自动映射 NapCat 的 `_send_group_notice` / `_get_group_notice` / `_del_group_notice`。
主动戳人推荐使用 `ctx.poke()` 或专用接口，而非旧的 `MessageSegment.poke()` 消息段。

```python
members = await ctx.get_members(no_cache=True)  # 当前群完整成员列表；私聊调用会报错
await ctx.poke()                               # 戳当前用户，自动选择群/好友接口
await ctx.poke(123456)                         # 戳指定用户
await ctx.connection.send_group_notice(ctx.group_id, "群公告")
await ctx.reply([MessageSegment.record("file:///C:/media/voice.mp3")])
await ctx.reply([MessageSegment.video("https://example.com/video.mp4")])
await ctx.connection.upload_group_file(ctx.group_id, "C:/files/report.pdf", "报告.pdf")
node = MessageSegment.node(ctx.user_id, ctx.nickname, "转发内容")
await ctx.connection.send_group_forward_msg(ctx.group_id, [node.to_dict()])
```

新增消息段构造：`record(file)`、`video(file)`、`file(file, name=None)`、`forward(message_id)`、
`node(user_id, nickname, content)`、`xml_card(payload)`、`dice()`、`rps()`。
本地文件路径指向 **NapCat 所在机器**；语音格式转换和不同消息段的发送能力取决于 NapCat。
发送正式群文件推荐 `upload_group_file`，私聊文件推荐 `upload_private_file`。


插件是启停和配置的完整功能包；Agent 工具是插件通过 `on_ai_tool(...)` 暴露的单个可调用动作。同一个插件可以同时包含命令、配置和多个 Agent 工具，例如语音插件提供 Qwen / IndexTTS 后端配置及 `voice__speak`。`category` 字段保留用于兼容和描述，不决定插件是否出现在插件管理页；工具目录按具体 `on_ai_tool` 注册项展示，并标出来源插件。

### 持久化任务来源

通过 `@plugin.task_source("reminders", "提醒与待办")` 声明异步回调 `async def console(operation, **values)`。来源名称为小写字母开头的字母/数字/下划线，最多 64 字符，不得重复；只注册管理能力，不授予模型工具权限。宿主不读取或导入某个具体插件的数据表。

回调支持三个 operation：list（before/limit/self_id/scene/scene_id/owner/state）、runs（task_id/before/limit）和 action（task_id/action/revision/acknowledge_duplicate）。list/runs 返回 `{items: [...], next_cursor: int|null}`；action 返回更新后的任务。任务字段及 API 见 [任务中心接口](api.md#插件任务中心)。回调以 KeyError 表示任务不存在，ValueError 表示版本/状态冲突。回调应仅做本地快速操作，不在管理请求内发送 QQ；版本检查和操作必须在同一事务中。

插件负责持久化任务、运行记录和重启恢复；发送前检查目标场景权限与连接，先落盘认领状态再调用发送接口，避免并发重复执行。未取得回执不能假定未送达；中断遗留应标记不确定，显式确认后方可重试。任务动作不得绕过插件或场景硬限制。daily 提供实现示例；其任务库独立于主数据库备份。

### 全局插件停用与周期任务

PermissionManager.plugin_enabled 将全局明确 false 作为优先硬限制，群覆盖不能绕过；全局无显式停用时，声明默认 false 的插件可由群单独开启。周期回调由宿主根据 plugin_loop_enabled 调度：实例默认启用或至少一个群单独启用才运行，全局 false 阻止新的周期回调。面向具体会话的任务仍需在执行前检查该群/私聊权限及连接，不得换账号代发。

停用不会卸载代码、删除数据或阻止生命周期初始化及管理读取；已开始的回调不会被强制取消，插件需在长操作后的实际发送前再次检查权限。此契约约束宿主入口，不能将任意插件自行创建的任务或低层外部调用描述为受宿主完整隔离。
## Agent 提示词片段与工具生命周期

插件可以通过声明式片段补充 Agent 策略，而不修改宿主的核心规则：

```python
plugin.agent_instruction(
    "repository_policy", tools=["read_repository"],
    text="分析仓库时先读取真实资料，不臆测未读取的内容。",
)
```

`tools` 是本插件的工具短名称，至少一个；所有关联工具都进入当前请求的权限白名单时才注入片段。片段名称在插件内唯一。宿主按插件名、片段名排序，组装核心规则后再追加片段；请求快照保留正文及 `插件名__片段名` 来源。插件停用、卸载或工具权限不满足后，后续请求不再包含该片段。已有请求快照保持不变，执行时仍复核实时权限。片段由受信任插件提供，不能授予工具权限。

工具钩子使用异步回调，只收到不可变的 `ToolEvent`，不会获得可修改的 Context 或连接：

```python
from fllbot.plugin import ToolContent, ToolRejection

@plugin.on_tool("before", name="check_policy", tools=["send"])
async def check_policy(event):
    # event.arguments 是 JSON 字符串；按需读取，避免在日志中泄露内容。
    if should_reject(event):
        return ToolRejection("不符合当前操作策略")

@plugin.on_tool("after", name="format_result", tools=["send"])
async def format_result(event):
    return ToolContent(event.content + "\n操作已处理。")

@plugin.on_tool("completed", name="observe", tools=["send"])
async def observe(event):
    plugin.logger.info("tool=%s status=%s", event.tool, event.status)
```

`before` 只能返回 `None` 或 `ToolRejection`，异常或超时阻止本次操作；参数校验先于钩子，钩子之后和执行登记之后都会复核权限。`after` 只处理已成功的执行，可返回 `None` 或 `ToolContent`，不能改变持久化的执行状态或静默标记。处理失败会返回结果处理错误，实际成功状态仍然保留，因此不会重做已完成的动作。

`completed` 在已登记执行的成功、失败或不确定结果保存后通知，只能返回 `None`；观察器异常记录到 trace，不覆盖结果。取消会向外传播，取消期间不保证完成通知。参数错误、策略拒绝、权限拒绝和缓存命中不产生新的完成通知；重复请求仍会经过执行前策略，但不会再次运行工具或结果处理。

钩子按插件名、钩子名排序；`tools` 可用本插件短名称或完整的 `插件名__工具名`，省略表示观察所有工具。仅启用且仍加载的插件参与；尚未启动的钩子在所属插件卸载或停用后跳过。每个钩子默认超时 1 秒，可设为 `(0, 5]` 秒，并受整轮截止时间约束。超时任务取消后仍占用执行容量直到退出；插件回调必须配合取消，不应从钩子直接发起外部动作。
