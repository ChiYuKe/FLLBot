# FLLBot

面向 [NapCat](https://github.com/NapNeko/NapCatQQ) 的自托管 QQ 机器人后端：**自带协议端连接、插件系统、权限与持久化、大模型多轮对话和管理 API**，不依赖 NoneBot / AstrBot 等现成框架。

- 协议：OneBot 11（反向 WebSocket，NapCat 主动连过来）
- 运行时：Python 3.11+，FastAPI + asyncio + SQLite
- 定位：本地 / 单机自用；内置管理后台，也可由桌面端（例如 `qq-bot-starter`）嵌入

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
- **持久化**：SQLite + `PRAGMA user_version` 迁移；插件配置、群配置、插件状态、聊天历史、消息统计
- **LLM 子系统**：OpenAI 兼容接口、上下文与人格管理，可供管理 API 和自定义插件使用
- **管理 API**：状态/连接/插件/群/配置/日志/统计/人格/插件配置、通用 OneBot 调用、KV 读写、SSE 实时事件流
- **可靠性**：事件短窗去重（防重连重放）、端口占用自检、处理器超时与异常隔离
- **开箱内置插件**：`basic`（包含 `ping`、`echo`、`help`、`plugins` 命令）和 `agent`（群级自然语言智能体，按需调用插件工具）

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
INFO  fllbot: FLLBot 0.1.0 已启动：WS /onebot/v11/ws | 插件 2 个 | LLM 未启用
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
| `llm` | `enabled` `base_url` `api_key` `model` `persona` ... | LLM 服务配置，供 API 与自定义插件使用 |

> 运行时用 `PATCH /api/config` 修改的配置会存进数据库 `meta` 表并覆盖文件配置，重启后仍然生效；`storage.path`、`plugin.dirs` 这类启动期路径需要重启才能生效。

## 内置命令

| 命令 | 说明 | 权限 |
| --- | --- | --- |
| `/ping`（`在吗` `pong`） | 连通性测试 | 所有人 |
| `/echo <内容>`（`复读` `repeat`） | 原样复读 | 所有人 |
| `/help`（`帮助` `菜单` `commands`） | 列出可用命令 | 所有人 |
| `/plugins`（`插件`） | 插件状态 | 所有人 |

LLM 客户端和管理 API 可供自定义插件调用。WebUI 将行为与群聊规则收进“配置”，将模型和人格作为可复用资源管理。智能体默认不读取群消息；开启后可选择仅 @、唤醒词或自动参与群聊，并勾选可调用的插件工具。

## 联网工具（web 插件）

`plugins/web/` 插件包让机器人和智能体能查实时信息，命令和智能体工具共用同一套实现：

| 命令 | 说明 |
| --- | --- |
| `/搜索 <关键词>`（`搜索` `联网`） | 网页搜索，返回标题、链接和摘要；默认必应，可切 Tavily / Serper / SearXNG |
| `/天气 <城市>`（`天气`） | 实时天气 + 未来几天预报，数据来自 Open-Meteo，免密钥 |
| `/现在 [城市]`（`时间` `几点`） | 当前日期和时间，可指定城市时区 |

智能体工具：`web__web_search`、`web__read_webpage`、`web__get_weather`、`web__get_current_time`。
需要阅读指定链接时授权「读取网页」；在「智能体 → 允许调用的工具」（或单群详细设置的“可用工具”）里勾选对应工具后，模型才会看到它们；配置项（搜索后端、
API Key、默认城市、预报天数、超时）都在插件配置里，详见 [plugins/web/README.md](plugins/web/README.md)。

## 写插件

统一采用**一个文件夹一个插件包**，先生成完整目录、包头、配置和文档：

```powershell
.venv/Scripts/python.exe -m fllbot plugin new welcome --description "入群欢迎"
.venv/Scripts/python.exe -m fllbot plugin check --builtin
.venv/Scripts/python.exe -m fllbot plugin list
```

`--template tool` 可生成带 AI 工具的模板。以下是包内 `plugin.py` 的装饰器用法示例：

```python
from fllbot.plugin import Context, Plugin, keyword

plugin = Plugin.from_manifest(__file__)

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

- `__init__.py` 导出 plugin，包头统一放在 plugin.toml，name 与文件夹名一致
- 装饰器：`on_command` / `on_message` / `on_notice` / `on_request` / `on_meta_event` / `on_startup` / `on_shutdown` / `on_loop`
- `ctx` 提供 `reply()` `finish()` `send()` `delete()` `get_member()`，以及 `db`（SQLite）、`settings`（插件配置）、`llm`、`config`、`is_superuser`
- 配置项用 `plugin.setting(...)` 声明，群里读取 `ctx.settings.get("key")` 会自动取本群覆盖值
- 详细 API 见 [docs/plugin-api.md](docs/plugin-api.md)

包内目录、文件头和文档要求见 [插件包规范](docs/plugin-packages.md)；宿主分层和新增功能放置约定见 [项目架构](docs/architecture.md)。

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

- **概览**：连接、模型、回复与插件就绪检查，直接定位缺少的配置
- **配置**：页内切换“群聊设置 / 默认行为”，只显示主动新增及已有的单群覆盖；未添加的群使用默认行为。常用项紧凑排列并自动保存，详细参数点击“编辑”；访问准入与默认访问范围也在同一张表内修改
- **模型服务**：页内切换“模型接入 / 对话测试”，维护模型资源并测试回答
- **人格库**：新建、编辑、删除自定义人格，设置默认人格，供群策略选择
- **插件**：管理已安装插件、配置与使用说明
- **数据与日志**：页内切换“运行日志 / 会话记录 / 用量统计”
- **底部系统设置**：连接地址、管理员、命令前缀、私聊、黑名单、上下文和日志级别
- 插件可直接启用/停用、编辑声明式配置，也能点「说明」查看插件 README（Markdown）；配置支持全局默认和群级覆盖
- 数据每 5 秒自动刷新；页面本身公开，数据请求都带令牌，也可不带参数打开后手工输入
- 不依赖 CDN，断网、内网环境都能用
- 沿用 QQ 桌面控制台样式，内嵌时实时同步桌面主题和字体；支持明暗主题、窄屏布局和 `#models` 等页面链接，切换页面保留未保存草稿
- 页面源码按 `fllbot/ui/` 下的 HTML、CSS、JS 分开维护，响应仍是零外部依赖的单个页面

更详细的操作说明见 [后台使用指南](docs/console.md)。

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
- 侧边栏「服务 → FLLBot」选择 FLLBot 目录后，会自动读取该目录的 `data/admin_token.txt`；
  连接地址与令牌只由管理服务持有，插件页上没有填写令牌或地址的表单
- 插件列表只做发现：按 `/api/plugins` 展示名称、版本、处理器、配置项数量与 README，
  不在控制台里提供启停或配置入口；要改就到「切换 WebUI → FLLBot WebUI」里操作

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
├── builtin_plugins/    # basic 与 agent
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
