# FLLBot

面向 [NapCat](https://github.com/NapNeko/NapCatQQ) 的自托管 QQ 机器人后端：**自带协议端连接、插件系统、权限与持久化、大模型多轮对话和管理 API**，不依赖 NoneBot / AstrBot 等现成框架。

- 协议：OneBot 11（反向 WebSocket，NapCat 主动连过来）
- 运行时：Python 3.11+，FastAPI + asyncio + SQLite
- 定位：本地 / 单机自用；管理 UI 交给桌面端（例如 `qq-bot-starter`），本项目只提供 API

```
┌──────────┐   反向 WS（OneBot 11）   ┌─────────────────────────────┐
│  NapCat  │ ───────────────────────▶ │ FLLBot                      │
│  (NTQQ)  │ ◀─────────────────────── │  ├ WS 服务端 + 事件分发      │
└──────────┘   API 调用（send_msg）    │  ├ 插件系统（热重载）        │
                                      │  ├ 权限 / 群开关 / 限流      │
┌──────────┐      HTTP + SSE          │  ├ SQLite（KV/群/历史/统计） │
│ 桌面端 UI │ ◀──────────────────────▶ │  └ LLM（OpenAI 兼容）        │
└──────────┘      /api/*              └─────────────────────────────┘
```

## 功能

- **反向 WebSocket 服务端**：任意路径接入、Token 鉴权、多账号连接、心跳超时清理、`echo` 关联的 API 调用
- **插件系统**：`plugins/*.py` 或 `plugins/<包>/__init__.py` 自动扫描、装饰器注册命令/消息/通知/请求、优先级与阻断、文件改动热重载、周期任务
- **插件声明式配置**：`plugin.setting(...)` 声明 schema（bool/int/float/select/list/secret），支持全局值与按群覆盖，管理端可直接渲染成表单（`/api/plugins/{name}/settings`）
- **插件 README**：支持 `plugins/<name>/README.md`、单文件插件同名 `.md` 或 `plugin.readme`，管理端用 `/api/plugins/{name}/readme` 读取（Markdown）
- **权限**：超管、群主/管理员、私聊开关、群白名单/黑名单、按群启用或停用某个插件、每用户冷却
- **持久化**：SQLite + `PRAGMA user_version` 迁移；插件配置、群配置、插件状态、聊天历史、消息统计、定时提醒
- **LLM 对话**：OpenAI 兼容接口，支持多 provider 路由（按场景/任务），多轮上下文裁剪、人格文件（`personas/*.md`）、@ 或前缀触发、长回复分条、限流、按群切换人格、token/费用用量统计
- **群治理**：入群欢迎/退群提示、违禁词（支持正则）、刷屏检测与自动撤回/禁言
- **定时提醒**：`/remind 10m 喝水`，按会话持久化，断线后恢复继续触发
- **管理 API**：状态/连接/插件/群/配置/日志/统计/人格/插件配置、通用 OneBot 调用、KV 读写、SSE 实时事件流
- **可靠性**：事件短窗去重（防重连重放）、端口占用自检、处理器超时与异常隔离
- **开箱内置插件**：`ping`、`echo`、`help`、`llm_chat`、`request_notify`、`group_guard`、`reminder`、`admin`

## 快速开始（Windows）

```powershell
# 1. 安装（Python 3.11+，推荐 3.12）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 2. 生成配置与示例插件
.\.venv\Scripts\python.exe -m fllbot init

# 3. 编辑 config.toml：至少填 bot.superusers；需要 AI 对话再填 llm.*
notepad config.toml

# 4. 自检并启动
.\.venv\Scripts\python.exe -m fllbot check
.\.venv\Scripts\python.exe -m fllbot run --root .
```

启动日志里会打印管理令牌（也写入 `data/admin_token.txt`）：

```
WARNING fllbot: 管理 API 令牌：xxxxx（文件：...\data\admin_token.txt）
INFO  fllbot: FLLBot 0.1.0 已启动：WS /onebot/v11/ws | 插件 6 个 | LLM 未启用
```

### 配置 NapCat

在 NapCat WebUI →「网络配置」中新增 **反向 WebSocket**：

| 项 | 值 |
| --- | --- |
| URL | `ws://127.0.0.1:6701/onebot/v11/ws` |
| Token | 与 `config.toml` 的 `server.access_token` 一致（留空则都不校验） |
| 消息格式 | `array`（默认，推荐） |
| 心跳 | 开启 |

保存并启用后，FLLBot 日志会出现 `NapCat 已连接：self_id=...`。

### 验证

在群里发 `/ping`，机器人回复 `pong`；或跑一次冒烟脚本：

```powershell
.\.venv\Scripts\python.exe tools\smoke_test.py
```

没有 QQ 环境时可以模拟一个 NapCat：

```powershell
.\.venv\Scripts\python.exe tools\fake_napcat.py --token <可选>
# 然后输入：/ping、at 你好、p hello、q 退出
```

## 配置

完整示例见 [config.example.toml](config.example.toml)，所有键都可用环境变量覆盖：
`FLLBOT_` + 段落 + `__` + 键名，例如 `FLLBOT_SERVER__PORT=6702`、`FLLBOT_LLM__API_KEY=sk-...`。

| 段落 | 关键项 | 说明 |
| --- | --- | --- |
| `server` | `host` `port` `ws_path` `access_token` `heartbeat_timeout` `call_timeout` | WS/HTTP 监听、鉴权、心跳与调用超时 |
| `admin` | `token` `token_file` | 管理 API 令牌；空则首次启动自动生成到文件 |
| `bot` | `superusers` `command_prefix` `allow_bare_command` `error_reply` | 超管 QQ、命令前缀、@ 时是否允许裸命令 |
| `permission` | `allow_groups` `deny_groups` `default_group_enabled` `allow_private` | 群白/黑名单与私聊开关 |
| `plugin` | `dirs` `builtin` `hot_reload` `max_concurrency` `handler_timeout` | 插件目录、内置插件、热重载与并发 |
| `storage` | `path` | SQLite 路径（改动需重启） |
| `logging` | `level` `dir` `json_lines` `ring_size` | 控制台 + JSONL 文件 + 内存环形缓冲 |
| `llm` | `enabled` `base_url` `api_key` `model` `trigger` `persona` ... | 大模型对话 |

> 运行时用 `PATCH /api/config` 修改的配置会存进数据库 `meta` 表并覆盖文件配置，重启后仍然生效；`storage.path`、`plugin.dirs` 这类启动期路径需要重启才能生效。

## 内置命令

| 命令 | 说明 | 权限 |
| --- | --- | --- |
| `/ping`（`在吗` `pong`） | 连通性测试 | 所有人 |
| `/echo <内容>`（`复读`） | 原样复读 | 所有人 |
| `/help`（`帮助` `菜单`） | 列出命令 | 所有人 |
| `/plugins`（`插件`） | 插件状态 | 所有人 |
| `/chat clear` | 清空本会话上下文 | 超管 / 群管 |
| `/chat persona [名称]` | 查看/切换本群人格 | 超管 / 群管 |
| `/requests` | 待处理好友/入群请求 | 超管 |
| `/approve <标记>` | 同意请求 | 超管 |
| `/reject <标记> [理由]` | 拒绝请求 | 超管 |
| `/guard` | 查看群治理配置 | 群管 |
| `/welcome on\|off\|set <模板>` | 入群欢迎开关与文案 | 群管 |
| `/badword add\|del\|list [词]` | 违禁词管理（`re:` 开头按正则） | 群管 |
| `/remind 10m 喝水` | 定时提醒 | 所有人 |
| `/remind list` / `/remind del <编号>` | 查看 / 删除提醒 | 所有人 |
| `/status` | 运行状态（连接、插件、LLM、消息量） | 超管 |
| `/reload <插件>` | 热重载插件 | 超管 |
| `/plugin on\|off <插件>` | 全局启停插件 | 超管 |
| `/group on\|off` | 启停本群的机器人 | 群管 |

LLM 对话默认 **群里 @ 机器人** 或 **私聊直接说话** 触发；改成前缀触发：`llm.trigger = "prefix"` + `llm.trigger_prefix = "ai"`。

> 群治理的欢迎语、违禁词、刷屏阈值都支持按群覆盖：群里用 `/welcome`、`/badword` 设置，
> 或调用 `PATCH /api/plugins/group_guard/settings`（带 `group_id`）。

## 写插件

在 `plugins/` 下新建文件（保存即热重载）：

```python
from fllbot.plugin import Context, Plugin, keyword

plugin = Plugin(name="welcome", version="0.2.0", description="入群欢迎")

# 声明配置项：管理端会自动渲染成表单，也可以按群覆盖
plugin.setting("text", type="text", label="欢迎语", default="欢迎 {nickname} 加入本群～", group_scoped=True)
plugin.setting("enabled", type="bool", label="启用", default=True, group_scoped=True)


@plugin.on_notice("group_increase")
async def welcome(ctx: Context) -> None:
    if not ctx.settings.get("enabled"):
        return
    text = ctx.settings.get("text").replace("{nickname}", str(ctx.user_id))
    await ctx.reply(text)


@plugin.on_command("card", aliases=("名片",))
async def card(ctx: Context) -> None:
    # /card 改成 xxx
    if not ctx.args:
        await ctx.reply("用法：/card 新名片")
        return
    await ctx.connection.set_group_card(ctx.group_id, ctx.user_id, ctx.args)


@plugin.on_message(rule=keyword("早"), priority=80, block=True)
async def morning(ctx: Context) -> None:
    await ctx.reply("早上好")
```

要点：

- 模块里必须导出 `plugin = Plugin(...)` 实例（名字唯一）
- 装饰器：`on_command` / `on_message` / `on_notice` / `on_request` / `on_meta_event` / `on_startup` / `on_shutdown` / `on_loop`
- `ctx` 提供 `reply()` `finish()` `send()` `delete()` `get_member()`，以及 `db`（SQLite）、`settings`（插件配置）、`llm`、`config`、`is_superuser`
- 配置项用 `plugin.setting(...)` 声明，群里读取 `ctx.settings.get("key")` 会自动取本群覆盖值
- 详细 API 见 [docs/plugin-api.md](docs/plugin-api.md)

## 管理 API

默认地址 `http://127.0.0.1:6701`，交互式文档 `/api/docs`，令牌用
`Authorization: Bearer <token>`（或 `X-Admin-Token`、`?token=`）。

```powershell
$token = Get-Content data\admin_token.txt
curl.exe -H "Authorization: Bearer $token" http://127.0.0.1:6701/api/status
```

常用端点：`/api/status`、`/api/connections`、`/api/plugins`、`/api/groups`、`/api/config`、
`/api/logs`、`/api/stats`、`/api/personas`、`/api/llm/test`、`/api/actions`、
`/api/events/stream`（SSE）。完整列表见 [docs/api.md](docs/api.md)。

LLM 子系统（多 provider、记忆、工具调用）的设计见 [docs/llm.md](docs/llm.md)。

## 自带 WebUI

FLLBot 自带一个单页控制台，零外部依赖、可直接被桌面端 iframe 嵌入：

```
http://127.0.0.1:6701/ui?token=<data/admin_token.txt 里的令牌>
```

- 展示运行时长、NapCat 连接、插件与分发统计、LLM 状态、最近日志
- 插件可直接启用/停用，也能点「说明」查看插件 README（Markdown）
- 数据每 5 秒自动刷新；页面本身公开，数据请求都带令牌，也可不带参数打开后手工输入
- 不依赖 CDN，断网、内网环境都能用
- 风格与桌面端对齐：深色卡片布局，支持 `?theme=light|dark`（桌面端会按当前主题自动带上）

桌面端的「切换 WebUI」菜单里已经加了 **FLLBot WebUI** 一项，点击后在面板内嵌打开。

## 与桌面端（qq-bot-starter）集成

FLLBot 在 OneBot 体系里扮演「机器人框架」的位置，但连接方向相反：**NapCat 主动连 FLLBot**。

桌面端接入建议：

1. **启动**：`<root>\.venv\Scripts\python.exe -m fllbot run --root <root> --port <port>`
2. **健康检查**：轮询 `GET /api/health`，`started` + `connections` 判断在线
3. **令牌**：读取 `<root>\data\admin_token.txt`，作为该账号的本地会话令牌
4. **日志**：`<root>\logs\fllbot.log`（JSONL）或直接采集进程 stdout
5. **NapCat 侧**：不要再用「HTTP 上报 / 正向 WS」，改为新增一条反向 WebSocket 指向
   `ws://127.0.0.1:<port>/onebot/v11/ws`
6. **状态展示**：`/api/status` 已聚合连接、插件、LLM、存储与分发统计；`/api/events/stream` 可直接做实时事件面板

### 已接入的插件管理页

桌面端 `qq-bot-starter` 的「插件管理 → FLLBot 插件」tab 已经接好：

- 管理服务通过 `/api/fllbot/*` 反向代理访问本机 FLLBot，前端 CSP 保持只放行 6700
- 侧边栏「服务 → FLLBot」选择 FLLBot 目录后，会自动读取该目录的 `data/admin_token.txt`，
  不需要手工填令牌；手工填写时用 Windows DPAPI 加密存在 `data/admin/fllbot.json`，不会回传到页面
- 插件列表支持启停；配置弹窗按 `plugin.setting(...)` 声明的 schema 自动渲染
  （bool / int / float / text / select / 多选 / 列表，密文不回显）
- 可切换「全局默认」或指定群做覆盖，只提交改动过的字段，避免把群覆盖写成整份全局值

## 目录结构

```text
fllbot/
├── app.py              # 装配与生命周期（FLLBot / create_app）
├── cli.py              # fllbot run | init | check
├── config.py           # 配置模型、TOML/环境变量合并、运行时覆盖
├── onebot/             # 协议层：事件模型、消息段、连接、反向 WS 服务端
├── plugin/             # 插件基类、上下文、加载器（热重载）
├── dispatcher.py       # 优先级 / 阻断 / 权限 / 限流
├── permission.py       # 超管、群开关、插件开关、冷却
├── storage/            # SQLite + 迁移 + 仓储
├── llm/                # 客户端、人格、多轮对话
├── api/                # 管理 REST + SSE
├── builtin_plugins/    # ping / echo / help / llm_chat / request_notify / group_guard / reminder / admin
└── personas/           # 内置人格
plugins/                # 用户插件（运行时加载，已 gitignore 之外）
personas/               # 用户人格（同名覆盖内置）
tools/                  # fake_napcat.py、smoke_test.py
tests/                  # pytest：单元 + API + 端到端 WS
```

## 开发

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

端到端测试会真的拉起 uvicorn 并用 WebSocket 客户端模拟 NapCat，覆盖「连接 → 收消息 → 插件 → 回调用 → 响应」全链路。

## 安全与隐私

- 默认只监听 `127.0.0.1`；不要直接暴露到公网。确实需要时务必设置 `server.access_token`。
- 管理 API 用独立令牌；`/api/health`、`/api/version` 无需令牌，其余都校验。
- 聊天记录与统计只存在本地 SQLite；LLM 调用会把上下文发给你配置的模型服务商，注意隐私。
- 不要在仓库里提交 `config.toml`、`.env`、`data/`、`logs/`（已在 `.gitignore`）。

## 已知限制

- OneBot 11 只实现了常用 API 子集（发消息、撤回、群成员/群列表、禁言、踢人、审批等），
  冷门接口可通过 `POST /api/actions` 直接透传，或在插件里调用 `ctx.connection.call(...)`。
- 单进程单事件循环；群数量特别大时需要把统计写入改成批量。
- 热重载是「删模块 + 重新执行源码」，不会清理插件自己创建的外部资源，卸载钩子里记得释放。
- 暂不支持频道（guild）事件与 NapCat 的 HTTP 上报模式。

## License

[MIT](LICENSE)
