# FLLBot

[![verify](https://github.com/ChiYuKe/FLLBot/actions/workflows/verify.yml/badge.svg)](https://github.com/ChiYuKe/FLLBot/actions/workflows/verify.yml)

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
- **插件系统**：自动扫描 `plugins/<插件名>/`，支持独立安装、升级和回滚，提供命令、事件、AI 工具、周期任务和热重载
- **插件声明式配置**：`plugin.setting(...)` 声明 schema（bool/int/float/select/list/secret），支持全局值与按群覆盖，管理端可直接渲染成表单（`/api/plugins/{name}/settings`）
- **插件 README**：支持 `plugins/<name>/README.md`、单文件插件同名 `.md` 或 `plugin.readme`，管理端用 `/api/plugins/{name}/readme` 读取（Markdown）
- **权限**：超管、群主/管理员、私聊开关、群白名单/黑名单、按群启用或停用某个插件、每用户冷却
- **持久化**：SQLite + `PRAGMA user_version` 迁移；插件配置、群配置、插件状态、聊天历史、消息统计
- **LLM 子系统**：OpenAI 兼容接口、上下文与人格管理，可供管理 API 和自定义插件使用
- **管理 API**：状态/连接/插件/群/配置/日志/统计/人格/插件配置、通用 OneBot 调用、KV 读写、SSE 实时事件流
- **可靠性**：有界事件队列与溢出统计、分阶段关闭与任务排空、SQLite 在线备份和校验后恢复
- **开箱内置插件**：`basic`（包含 `ping`、`echo`、`help`、`plugins` 命令）、`agent`（群级自然语言智能体）、`text`（智能体文本、图片与架构图排版工具）、`github`（公开仓库读取）、`files`（QQ 文本与代码附件读取）、`oni_knowledge`（本地缺氧源码检索与读取；工具需在后台授权）、`workspace`（沙盒工作区文件读写与受控网页读取）和 `screen`（宿主截屏，默认关闭）
- **群聊参与与执行沙盒**：自动参与先用确定性信号评分（@、唤醒词、提问、接话加分；纯语气词、复读、刷屏减分），通过后才请求模型；AI 工具声明能力与风险，执行前统一裁决能力开关、工作区路径、主机白名单、发起者权限与自动参与边界，拒绝不产生外部动作
- **工具目录与执行效率**：宿主缓存插件工具快照和模型定义模板，插件/权限/Schema 变化后自动重建；执行期只解析目标工具并实时复核授权，同一轮内相同参数的纯读取调用复用结果，减少重复 I/O、存储写入和提示词开销
- **工具调用体验**：参数校验失败时按 Schema 做有限无损自愈（数字字符串、单值→数组、JSON 字符串→对象等）并再次严格校验；失败结果附中文 `message` 与字段级修正建议；同轮相同参数只读调用超过 12 次直接拒绝；已完成发送/保存的工具用一句自然确认直出，省掉一轮模型往返；规则引导模型不宣告工具、同轮并行只读查询、失败用人话解释
- **私聊智能体**：默认关闭；开启后私聊消息走同一套自然语言智能体，使用独立的全局工具白名单，私聊历史与群历史/群记忆完全隔离，工具执行仍经沙盒裁决；可按「Bot 账号 + 用户」设置每日 Token/费用额度

## 快速开始（Windows）

需要架构图功能时，额外安装 Node.js 18+ 并加入 PATH；ELK.js 布局引擎随包提供，无需 npm 安装。
结构树、普通文本图片与机器人基础功能无需 Node.js。

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

## 项目目录

| 路径 | 用途 |
| --- | --- |
| `fllbot/` | 框架、管理 API、WebUI 和插件 SDK。随附插件放在 `fllbot/builtin_plugins/`。 |
| `plugins/<插件名>/` | 外置插件的清单、功能代码、文档和静态资源。可通过 `plugin.dirs` 指定其他源码目录。 |
| `data/plugins/<插件名>/` | 插件自己的数据库、缓存和生成文件，通过 `ctx.data_dir` 或 `plugin.data_dir` 访问。 |
| `data/fllbot.db` | 默认宿主数据库，保存插件设置、权限、群配置和聊天记录。 |
| `services/` | 独立运行的外部服务，例如 TTS。 |
| `tests/`、`scripts/` | 自动化测试与项目验证入口。 |

升级或移除插件时，插件数据按原插件名保留。完整备份实例时应同时保存宿主数据库、`data/plugins/` 和 `personas/`；内置数据库备份只覆盖主库。完整约定见 [插件包规范](docs/plugin-packages.md)。

修改项目后，在 Windows 上运行 `.\scripts\verify.ps1`，统一检查架构、管理 API 作用域、插件包、Ruff、前端 JS 语法（有 Node 时）和测试；CI 在 Windows 上对 `master` 推送与 PR 跑同一脚本。

## 配置

完整示例见 [config.example.toml](config.example.toml)，所有键都可用环境变量覆盖：
`FLLBOT_` + 段落 + `__` + 键名，例如 `FLLBOT_SERVER__PORT=6702`、`FLLBOT_LLM__API_KEY=sk-...`。

| 段落 | 关键项 | 说明 |
| --- | --- | --- |
| `server` | `host` `port` `ws_path` `access_token` `heartbeat_timeout` `call_timeout` | WS/HTTP 监听、鉴权、心跳与调用超时 |
| `admin` | `token` `token_file` | 管理 API 令牌；空则首次启动自动生成到文件 |
| `bot` | `superusers` `command_prefix` `allow_bare_command` `error_reply` | 超管 QQ、命令前缀、@ 时是否允许裸命令 |
| `permission` | `allow_groups` `deny_groups` `default_group_enabled` `allow_private` | 群白/黑名单与私聊开关 |
| `plugin` | `dirs` `builtin` `hot_reload` `max_concurrency` `max_pending_events` `handler_timeout` | 插件目录、内置插件、热重载、并发与事件队列上限 |
| `storage` | `path` `backup_dir` | SQLite 路径只在启动来源修改；备份目录可在线更新 |
| `logging` | `level` `dir` `json_lines` `ring_size` | 控制台 + JSONL 文件 + 内存环形缓冲 |
| `llm` | `enabled` `base_url` `api_key` `model` `persona` ... | LLM 服务配置，供 API 与自定义插件使用 |
| `sandbox` | `allow_write` `allow_network` `allow_email` `allowed_hosts` `workspace` `max_*` | 智能体工具的能力开关、工作区、主机范围与单轮额度 |

> 运行时用 `PATCH /api/config` 修改的参数会保存到数据库并在重启后保留。监听、数据库位置、插件加载、日志文件和管理入口等启动参数必须在配置文件/启动命令中修改并重启；在线修改返回 409，整次请求不保存。旧版未生效的启动覆盖会归档保留，后台可只读查看。完整范围见 [配置与数据作用域](docs/admin-scope.md)。

## 内置命令

| 命令 | 说明 | 权限 |
| --- | --- | --- |
| `/ping`（`在吗` `pong`） | 连通性测试 | 所有人 |
| `/echo <内容>`（`复读` `repeat`） | 原样复读 | 所有人 |
| `/help`（`帮助` `菜单` `commands`） | 列出可用命令 | 所有人 |
| `/plugins`（`插件`） | 插件状态 | 所有人 |

LLM 客户端和管理 API 可供自定义插件调用。WebUI 将行为与群聊规则收进“配置”，将模型和人格作为可复用资源管理。智能体默认不读取群消息；开启后可选择仅 @、唤醒词或自动参与群聊，并勾选可调用的插件工具。自动参与会先经过确定性评分，明显无需接话的消息不请求模型；工具执行前还会按沙盒策略自动判断能否执行。

## 私聊智能体

内置 `agent` 插件的私聊能力默认关闭，开关与工具白名单都在「配置 → 默认行为 → 私聊智能体」（全局设置，不随群变化）：

- 开启“私聊自然语言响应”后，好友私聊里的普通文字交给同一套智能体处理；模型、额度、人格与计费沿用 Bot 默认设置和全局聊天配置；
- 私聊工具白名单与群聊独立：只有这里勾选、插件全局启用且用户权限允许的工具才会出现，执行仍经 ToolExecutor 与沙盒裁决；
- 私聊只读取对方 QQ 下的私聊历史，群聊历史与群聊记忆（含摘要、`agent__search_history`）不会进入私聊；私聊历史也不会进入任何群聊请求；
- 私聊消息视为明确请求，不做自动参与评分，也不提供静默工具；群级设置（回复方式、冷却、工具白名单、单群沙盒覆盖）都不作用于私聊，工作区按 `private_<QQ号>` 隔离、沙盒按全局 `[sandbox]` 解析；
- 可按「Bot 账号 + 私聊用户」设置每日 Token/费用额度（`llm.private_daily_token_limit` / `private_daily_cost_limit`，默认 0 不限），与群额度互不影响，超限时在请求模型前拒绝；
- 任务控制同样可用：`/任务进度`、`/任务取消`、`/任务补充 新要求`，以及执行期间直接发消息追问；触发原因会写入“消息追踪”。
  完整说明见 [agent 插件说明](fllbot/builtin_plugins/agent/README.md)。

## 联网工具（web 插件）

`plugins/web/` 插件包让机器人和智能体能查实时信息，命令和智能体工具共用同一套实现：

| 命令 | 说明 |
| --- | --- |
| `/搜索 <关键词>`（`搜索` `联网`） | 网页搜索，返回标题、链接和摘要；默认必应，可切 Tavily / Serper / SearXNG |
| `/天气 <城市>`（`天气`） | 实时天气 + 未来几天预报，默认腾讯 QQ 天气（国内直连、免密钥），可切 Open-Meteo；默认附天气卡片图 |
| `/现在 [城市]`（`时间` `几点`） | 当前日期和时间，可指定城市时区 |

智能体工具：`web__web_search`、`web__read_webpage`、`web__get_weather`、`web__weather_card`、`web__get_current_time`。
需要阅读指定链接时授权「读取网页」，需要天气图片时授权「天气图片」；在「智能体 → 允许调用的工具」（或单群详细设置的“可用工具”）里勾选对应工具后，模型才会看到它们；配置项（搜索后端、
API Key、天气服务、默认城市、预报天数、超时）都在插件配置里，详见 [plugins/web/README.md](plugins/web/README.md)。

## 图片生成（imagegen 插件）

`plugins/imagegen/` 让机器人和智能体按一句话画画：表情包、头像、情绪图都可以。

- **默认本地模式**：用 Pillow 直接画机器人自己的猫娘形象（银灰发、丸子头、猫耳、黑旗袍、大尾巴），
  不需要密钥、不需要联网，也不落盘。支持 11 种表情（normal/happy/angry/sad/shy/sleepy/surprised/
  smug/love/cry/blank）、6 种配色（night/sakura/rain/sunset/mint/mono）、最多 16 字气泡和随机种子；
  提示词里的“生气 / 雨夜 / 樱花 / 委屈”等关键词会自动选表情和配色。
- **入口**：`/画 <描述>`（`画图` `绘图` `draw`）或智能体工具 `imagegen__draw`；工具还能指定
  `expression`、`palette`、`caption`、`seed`，换一张时换 seed 即可。
- **可选远程模式**：管理员可切换 `openai`（OpenAI 兼容 `/images/generations`）、`siliconflow`
  （Kolors）或 `sdwebui`（本机 Stable Diffusion WebUI），画任意画面；远程结果同样校验后转 PNG 发送。
- 本地模式画不了别的角色、写实照片或复杂构图，遇到这类请求会如实说明；不会用文字图冒充。
- 工具声明 `qq.send`，自动参与（没有 @ 或唤醒词）时拒绝生成；未在「智能体 → 允许调用的工具」
  里授权时模型看不到它。配置与后端示例见 [plugins/imagegen/README.md](plugins/imagegen/README.md)。

## 邮件发送（email 插件）

`plugins/email/` 让智能体通过 QQ 邮箱发纯文本邮件。群友邮箱通常就是 `QQ号@qq.com`，所以工具直接收 QQ 号，
留空收件人时默认发给当前发言人自己的邮箱：

- 智能体工具：`email__send_email`（主题、正文、可选收件人 `to`）；收件人可写 QQ 号或完整 QQ 邮箱，多个用逗号/空格/分号分隔；
- 配置：发件邮箱、SMTP 授权码、服务器/端口/加密方式、允许的收件域名、单封收件人数与正文字数上限；
- 凭据准备：在 QQ 邮箱「设置 → 账户」开启 SMTP 并生成授权码（不是登录密码）；
- 默认 `[sandbox].allow_email = false`，需管理员开启；单群可用「沙盒邮件」覆盖；自动参与不会发邮件，必须由群友明确请求；
- 邮件只能纯文本、无附件；默认只允许 `qq.com` 收件域名。完整说明见 [plugins/email/README.md](plugins/email/README.md)。

## 戳一戳（poke 插件）

`plugins/poke/` 让机器人回应群友的戳一戳（NapCat 的 `notice.notify.poke`）：

- 别人在群里戳机器人时，默认先回戳发起者一次，再按当前人格回复一到两句；
- 群级设置 `poke_reply`、`poke_back` 控制是否回应和回戳，`poke_reply_seconds` / `poke_back_seconds` 控制冷却（默认 10 秒）；
- 智能体工具 `poke__poke` 需在「允许调用的工具」里勾选；只限当前会话，自动参与时拒绝执行；
- 私聊戳、群友互戳不会触发；被戳回应走 `ctx.llm`，不依赖 `agent` 插件。完整说明见 [plugins/poke/README.md](plugins/poke/README.md)。

## 情绪与悄悄话（emotion 插件）

`plugins/emotion/` 让机器人有按群记录的情绪，并在群友正聊天时偶尔发一条只有对方能看到的群临时会话：

- 被夸、被凶、被复读、深夜、被 @、被戳都会轻微影响心情和精神，情绪按半衰期缓慢回落；
- `/情绪` 查看当前心情与额度，`/哄哄` 可以哄它，`/悄悄话 关闭` 可按用户关闭主动私聊；
- 只挑最近和机器人说过话、并且正在群里发言的人，带同群间隔、个人冷却、每群/全局每日额度和安静时段；
- 勾选 `emotion__read_mood` 后会给智能体附加情绪语气提示，减少客服腔；`emotion__send_whisper` 让模型在群友明确要求时发临时会话；
- 模型不可用时使用内置情绪化短句；发送失败只进入冷却，不自动重试。完整说明见 [plugins/emotion/README.md](plugins/emotion/README.md)。

## 长期记忆（memory 插件）

`plugins/memory/` 让智能体记住群友本人明确要求记住的事实和偏好，并按「机器人账号 + 群/私聊 + 用户」三层隔离：

- 工具 `memory__remember`、`memory__recall`、`memory__forget`，需在「智能体 → 允许调用的工具」里勾选；
  只有群友明确说“记住……”时才写入；自动参与（未 @、未叫到机器人）时由宿主沙盒按 `memory.write` 能力统一拒绝（返回 `tool_blocked`），不再由插件自检；
- 命令 `/记忆`（`memory` `回忆`）查看本人当前会话的记忆，`/忘记 <编号|全部>`（`forget`）删除本人记录；
- 默认每位用户 50 条、单条 200 字、保留 180 天；重复内容自动去重，写入时和每小时清理过期记忆；
- 密码、口令、令牌、验证码、证件号等凭据会被确定性拒绝；数据保存在 `data/plugins/memory/` 的独立数据库，
  不写宿主数据库。关闭 `auto_hint` 后不再注入“何时记忆”的提示词片段，工具仍可调用。
  完整说明见 [plugins/memory/README.md](plugins/memory/README.md)。

## 沙盒工作区（workspace 插件）

`workspace` 给智能体一个受限的动手环境，工具都通过宿主 `ctx.sandbox` 访问资源：

| 工具 | 能力 | 说明 |
| --- | --- | --- |
| `workspace__list_files` | `fs.read` | 列出工作区内相对目录 |
| `workspace__read_file` | `fs.read` | 读取工作区内文本文件 |
| `workspace__write_file` | `fs.write` | 写入文本（默认关闭，需管理员开启；自动参与时不会执行） |
| `workspace__fetch_url` | `net.read` | 抓取公开 http(s) 文本页面 |
| `workspace__stat` / `search` / `make_dir` / `move` / `copy` / `delete` | `fs.read` / `fs.write` | 查看、搜索、建目录、移动、复制、删除工作区文件 |
| `workspace__download` | `net.read` + `fs.write` | 从允许的 http(s) 地址下载文件到工作区 |
| `workspace__send_file` | `fs.read` + `qq.send_file` | 把工作区文件作为附件发送到当前会话 |

工作区默认 `data/agent_workspace/`，`workspace_scope = "scene"` 会按群/私聊分目录，避免跨群读取；
绝对路径、`..`、软链接逃逸和 `.env`、数据库、令牌、日志等敏感文件会被拒绝。联网受主机白/黑名单
与内网地址检查约束，每次跳转重新校验。

策略在 `config.toml` 的 `[sandbox]`（`allow_write`、`allow_email`、`allowed_hosts`、大小与额度等），
单群可用 `sandbox_write` / `sandbox_network` / `sandbox_email` 收紧或放宽。完整说明见
[群聊智能体与执行沙盒](docs/agent-sandbox.md)，全部能力与刻意不做的边界见
[群聊 Agent 能力清单](docs/agent-capabilities.md)。

## 生成并发送代码/文本文件

`text` 插件新增 `text__send_text_file`：让智能体把生成的代码或文本原样保存为附件发到当前会话。

- 支持 `.py`、`.js`、`.ts`、`.cs`、`.java`、`.go`、`.rs`、`.json`、`.yaml`、`.sh`、`.ps1`、`.md` 等源码/配置扩展名
- 文件名必须带扩展名，不能包含路径；正文最多 30000 字符，文件最大 2 MiB
- 需要“指定大小的测试文件”（如 1 MB 的 `fault.cpp`）时传 `target_bytes`，工具只用注释或空行补足到该字节数，不改动原内容
- 拒绝 `.exe`、`.dll`、`.bat`、`.cmd`、`.zip` 等可执行文件、启动器、压缩包和隐藏文件名
- 与另外两个文件工具一样声明 `qq.send_file`，自动参与时会被沙盒拒绝，需群友 @ 或唤醒词明确请求
- 正文确实超过 30000 字符（真实内容，不是补大小）时，可先用 `workspace__write_file` 分段写入工作区，再用 `workspace__send_file` 发送（需开启沙盒写入）

启用后在群里说“把这个写成 main.py 发我”即可；文件工具需要在「智能体 → 允许调用的工具」中授权。

## 生成 Word / Excel / PPT（docs 插件）

`plugins/docs/` 让群友说一句“做个表格 / 报告 / PPT 发我”就能收到真正的 Office 文件：

- 工具 `docs__write_docx`、`docs__write_xlsx`、`docs__write_pptx`，生成 `.docx` / `.xlsx` / `.pptx`
  并用 QQ 文件附件发送到当前会话；需要在「智能体 → 允许调用的工具」里授权；
- 三个工具都声明 `qq.send_file`，自动参与（未 @、未叫到机器人）时被沙盒拒绝，必须群友明确请求；
- 只接受受控文本结构（Word 的标题/正文/要点/表格、Excel 的行列数据、PPT 的页标题/要点），
  不支持图片、超链接、公式、任意 XML 或模板；文件名严格清洗，禁止路径、隐藏名和系统保留名；
- 默认限制：单文件 5 MB、总字数 50000、Word 60 章节、PPT 30 页、表格 2000 行 × 50 列、20000 单元格；
  生成文件在 `data/plugins/docs/files/`，发送成功后按 `retention_days`（默认 3 天）清理，加载时与每 6 小时也会清理旧文件；
  自动参与由宿主沙盒与插件兜底双重拒绝；
- 依赖 python-docx、openpyxl、python-pptx（已写入 `pyproject.toml`），缺失时插件加载被清单检查阻止。
  完整参数、错误码与配置见 [plugins/docs/README.md](plugins/docs/README.md)。

## 截屏（screen 插件，默认关闭）

`screen` 提供 `screen__capture_screen`（截取宿主屏幕并发送）与 `screen__capture_screen_file`
（截取并保存到沙盒工作区）。它声明高风险能力 `screen.capture`，默认**不可用**：

- 需要 `[sandbox].allow_screen = true`；保存文件还需要 `allow_write = true`；
- 默认要求超级管理员发起；关闭该限制后仍至少要求群主/管理员，普通成员不能截屏；必须是 @ 或唤醒词明确请求；自动参与不会截屏；
- 单群可用 `sandbox_screen` 收紧或放开。

截取的是运行 FLLBot 的电脑屏幕，不是提问者的设备；截图可能包含桌面隐私，开启前请确认使用场景。

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

默认地址 `http://127.0.0.1:6701`，交互式文档 `/api/docs`。令牌建议通过
`Authorization: Bearer <token>` 发送（也支持 `X-Admin-Token`）；`?token=` 仅保留旧客户端兼容，避免用于可被记录或转发的 URL。

```powershell
$token = Get-Content data\admin_token.txt
curl.exe -H "Authorization: Bearer $token" http://127.0.0.1:6701/api/status
```

常用端点：`/api/status`、`/api/connections`、`/api/plugins`、`/api/groups`、`/api/config`、
`/api/logs`、`/api/stats`、`/api/personas`、`/api/llm/test`、`/api/actions`、
`/api/storage/backup`、`/api/events/stream`（SSE）。完整列表见 [docs/api.md](docs/api.md)。

### 数据库备份与恢复

运行中的机器人可以通过 `POST /api/storage/backup` 或命令行创建一致性备份。恢复前先停止 FLLBot；恢复命令会验证备份并在替换前自动保留当前数据库副本：

```powershell
python -m fllbot backup --root .
python -m fllbot restore --root . --source data\backups\fllbot-<时间戳>.db --confirm
```

备份目录可通过 `storage.backup_dir` 设置；命令行也支持 `--destination` 指定单次备份位置。

LLM 子系统（多 provider、记忆、工具调用）的设计见 [docs/llm.md](docs/llm.md)。

## 自带 WebUI

FLLBot 自带一个单页控制台，零外部依赖、可直接被桌面端 iframe 嵌入：

```
http://127.0.0.1:6701/ui?token=<data/admin_token.txt 里的令牌>
```

- **概览**：连接、模型、回复与插件就绪检查，直接定位缺少的配置
- **配置**：页内切换“群聊设置 / 默认行为”，只显示主动新增及已有的单群覆盖；未添加的群使用默认行为。常用项紧凑排列并自动保存，详细参数点击“编辑”；访问准入与默认访问范围也在同一张表内修改
- **模型服务**：页内切换“模型接入 / 对话测试”，维护模型资源并测试回答
- **人格库**：人格全部由你创建，可新建、编辑、删除并设置默认人格，供群策略选择；「说明」列出 `{bot_name}` 等可用变量
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
├── trigger_policy.py   # 触发决策与自动参与状态
├── participation.py    # 自动参与评分（确定性、可解释）
├── sandbox/            # 能力声明、操作裁决、工作区文件与受控联网
├── tool_registry.py    # 工具目录快照、定义模板与变更重建
├── storage/            # SQLite + 迁移 + 仓储
├── llm/                # 客户端、人格、多轮对话
├── api/                # 管理 REST + SSE
├── builtin_plugins/    # basic、agent、text、github、files、oni_knowledge、workspace 与 screen
plugins/                # 用户插件（运行时加载，已 gitignore 之外）
personas/               # 用户人格（全部由你创建，程序不内置人格）
tools/                  # fake_napcat.py、smoke_test.py
tests/                  # pytest：单元 + API + 端到端 WS
```

## 开发

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe scripts/check_architecture.py
.\.venv\Scripts\python.exe scripts/check_admin_scope.py
```

端到端测试会真的拉起 uvicorn 并用 WebSocket 客户端模拟 NapCat，覆盖「连接 → 收消息 → 插件 → 回调用 → 响应」全链路。
`tests/test_agent_scenarios.py` 另外用假连接与假模型从群消息入口回归智能体的跨模块行为：工具授权与执行期吊销、
沙盒自动参与/明确请求裁决、`stay_silent`、工具失败/超时/未发送的执行事实与不重放、重复调用提醒、追问并入与取消、
预算折叠后的工具配对、引用与 @ 目标记录。`tests/test_tool_resilience.py` 与 `tests/test_agent_direct_reply.py`
覆盖参数自愈、失败中文说明、重复只读上限，以及整批 silent 才直出、混合批次继续模型、失败批次不提前收尾。

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
