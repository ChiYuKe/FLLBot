# 管理 API

## 控制台接口

### 数据库备份管理

- `GET /api/console/backups?before=<name>&limit=30`：分页列出当前备份目录中 FLLBot 命名的 `.db` 文件。返回 items、next_cursor、active、scope 和范围说明；每项包含文件名、大小、修改时间、当前进程缓存的校验状态与数据库版本。未校验不表示损坏；文件指纹变化或进程重启后需重新校验。
- `POST /api/console/backups`：返回 202 和作业 id/status。`GET /api/console/backup-jobs/{id}` 查询 pending/running/completed/failed；完成后提供 result。只允许一个活动备份，同步旧接口共用此限制，冲突返回 409。
- `POST /api/console/backups/{name}/validate`：执行 SQLite 完整性与支持版本检查。`GET .../{name}/download` 下载备份，需管理鉴权，不支持任意路径或非备份命名文件；返回禁止缓存的附件。
- `GET /api/console/backups/{name}/restore-guide`：返回 requires_shutdown、步骤及经过 PowerShell 单引号转义的恢复命令，只展示，不执行恢复。

备份作业在进程内执行，客户端取消请求不会取消已提交的文件复制；正常关闭等待备份任务结束。近期保留 50 个作业状态，重启后不能再查询原作业，应刷新文件列表核对，不能据此重复创建。完成的文件是持久化证据。`POST /api/storage/backup` 保留同步等待并返回原有 ok/path/元数据字段，也由同一管理器执行。

下载的数据库包含会话、插件 KV 和运行配置，可能含密钥；不包含 TOML、人格文件、插件源码/文件数据或外部模型，不能作为完整实例备份。恢复继续使用停机 CLI：校验源、获取数据库进程锁、创建现有数据库副本，再替换。后台不提供运行中恢复或删除备份接口。

### 插件外部服务状态

`GET /api/console/services` 只读取当前已加载插件声明及检查缓存，不联网。条目包括 id/title/plugin/selected/plugin_enabled/state/code/checked_at/latency_ms/stale/checking；超过 60 秒标记状态过期。插件热重载或卸载后不沿用旧对象的检查结果。

`POST /api/console/services/check` 接受可选 `{service_id}`；不指定时检查全部已声明项目。检查最多并发 4 项，每项使用插件声明的超时（不超过 10 秒），同时发起另一轮返回 409。不接受临时 URL，不会自动启动服务或生成内容。state 为 unknown/ready/loading/idle/unavailable/error；插件返回的其他内容与异常字符串不透出到该接口。

语音插件声明 Qwen 与 IndexTTS 两个 `/health` GET 检查，标明当前所选后端。idle 表示空闲卸载，可在生成请求到来时加载，不能当成服务故障；ready 只代表该时刻健康接口返回就绪，不验证实际语音输出。

### 消息处理链路诊断

`GET /api/console/traces` 支持 `self_id/scene/scene_id/message_id/outcome/before/limit`。scene 为 group/private；outcome 为 sent、warning、failed、skipped、processed、cancelled、dropped；limit 为 1–100，默认 30。响应为 `items/next_cursor/recorder/retention_days/max_records`；recorder 包含 pending、capacity、dropped 和 write_failures，本次运行重新计数。

`GET /api/console/traces/{trace_id}` 读取详情，trace_id 为 32 位小写十六进制；不存在或超过保留期返回 404。详情包含账号、场景、场景 ID、协议消息 ID、开始时间、总耗时、结果、truncated 和 steps。每个阶段包含 stage、code、status、component、elapsed_ms，可选 duration_ms。诊断 ID 同时附加到该任务产生的 JSONL/内存日志，近期运行日志可按此 ID 搜索；日志保留范围与诊断表不同。

记录覆盖宿主接收、重复消息、排队/丢弃、准入、处理器匹配与权限、智能体触发/概率/冷却/沉默、模型重试及备用服务、工具调用、宿主发送与 OneBot 发送响应。只记录元数据，不保存消息正文、模型内容、工具参数/结果、URL、密钥或异常字符串。插件自行创建并脱离当前消息生命周期的后台任务不继续写入已完成的消息记录；外部服务内部执行过程也不在该链路中。

数据库版本 8 增加 message_traces 表与索引，查询只展示最近 7 天，写入时清理过期与超过最近 10000 条的记录。每条最多 100 个阶段，超过时标记 truncated，但汇总结果仍统计后续阶段。后台写入队列最多 500 条，满时丢弃新诊断并计数，不阻塞消息队列；写入失败只计数与记录简短运行日志。未完成处理的消息尚无终态记录，异常进程退出可能丢失尚未落库的诊断。

sent 仅表示观察到宿主/协议发送调用成功；warning 表示已观察到成功发送，但链路中也有失败（例如备用模型恢复或转发降级）。processed 表示处理器结束但没有观察到发送，不能据此断言插件没有执行外部动作。诊断结果不保证消息最终送达或已读。

### 首次接入与共享保存协调

`GET /api/console/setup?self_id=<QQ号>&group_id=<测试群号>` 返回所选账号、接入步骤、`completed_checks/total_checks/configuration_ready` 及 `verification`。人格为选填项，不计入必要配置数量；`verification.status` 始终为 `manual`，静态检查通过后仍需在真实群验证。该接口只读，不请求模型、发送 QQ 消息或验证群成员关系，也不自动启用群。

控制台与通用群配置接口现在共用存储实例的设置写锁；群权限、插件设置和账号默认值的服务层写入同样使用此锁，避免检查版本和保存之间插入另一条管理写操作。读取群配置返回独立副本，修改返回对象不会提前改变运行状态。直接操作数据库或插件内部 KV 不属于此管理契约；管理端通用 KV 写入由 API 专用事务保存并记录不含值和键名的作用域审计。

`GET /api/groups/{group_id}/config` 返回 `revision`，对应 PATCH 和 `/groups/{group_id}/enabled` POST 支持 `If-Match`；深合并在共享锁内执行。`GET/PATCH /api/config` 返回全局 `revision`，PATCH 支持 `If-Match`。`GET/PUT /api/console/chat` 保留原 JSON 结构，通过 `ETag` 响应头提供版本，PUT 接受 `If-Match`。版本过期统一返回 409；不传版本保留旧客户端兼容性。

全局配置先生成候选值并校验，持久化成功后才发布到运行态；持久化失败不提前修改内存配置。版本检查由每个编辑器保存其读取时的版本，不能使用其他页面后来读取的版本替代旧表单的版本。

### 群列表状态与共享资源引用

`GET /api/console/groups` 支持 `self_id` 选择生效设置的机器人视角；群详情及群设置、准入、新增/移除规则、批量准入响应同样支持此查询参数。未指定时优先选择在线主账号，否则使用第一个已保存的账号。此参数只改变查看视角，群覆盖依旧共享。

群详情新增 `needs_attention` 和 `diagnostic_status`（`ready/attention/disabled`）。只有群准入、群开关、AI 开关和群响应开启时，才把连接或配置不就绪、已授权工具不可用计入 attention。ready 仅表示这组静态检查通过，不验证模型实际连通性或账号群成员关系。

`GET /api/console/resources/{kind}/{name}/references` 返回 `kind/name/count/items`，kind 为 `persona` 或 `provider`。引用项包含 `scope/object_id/field/label/target`，scope 为 `system/bot/group`。只统计明确保存的绑定，不展开继承关系，不返回配置内容与密钥；资源不存在返回 404。

人格 DELETE 在仍有机器人或群引用时返回 409，保留文件和设置。只有系统默认引用时，删除仍按原有行为清空默认人格并使用兜底提示词。

### 群设置版本与变更记录

群详情和批量群列表新增 `revision`。群设置 PATCH、单群准入 PATCH、新增/移除群规则使用请求头 `If-Match: <revision>` 做版本检查。默认访问规则 GET 同样返回 `revision`，PATCH 支持相同请求头。批量准入 PATCH 可提供 `revisions: {"群号": "revision"}`，必须覆盖全部所选群；全部版本通过后才修改。

版本过期返回 409，当前请求不提交改动。控制台与通用群配置、全局配置及插件设置共用设置写锁，避免两个窗口同时通过旧版本检查。旧客户端不传版本仍可按原方式调用，但不具备并发覆盖保护。群配置、旧版群插件配置、默认群开关或访问规则变化会使旧群版本失效。

`GET /api/console/groups/{group_id}/changes?limit=20&before=<id>` 返回 `items` 和 `next_cursor`，按 ID 倒序；limit 范围 1–100。每条包含 `id/group_id/action/fields/created_at/actor`，actor 固定为 `instance`，不标识具体人员。群号 0 表示全局配置变化，可能影响本群，并非对本群的单独修改。

数据库版本 7 新增变更表与触发器，群记录写入与其变更记录在同一 SQL 事务中生效。记录迁移后的群登记、群配置修改、群记录删除和全局配置持久化；不会补录旧历史，不保存配置内容、密钥或提示词，不提供一键回退。插件设置、Bot 默认值等其他对象尚未纳入记录。记录随数据库备份保存。

### 生效配置与不回复诊断

`GET /api/console/groups/{group_id}?self_id={QQ号}` 保留原字段，并在 `effective_policy` 中增加：

- `sources`：生效字段的来源，`system` 系统默认、`bot` 机器人默认、`group` 群覆盖、`legacy_group` 旧版群插件配置、`nickname` 账号昵称、`unresolved` 未选择账号。旧版群插件配置继续按原优先级生效。
- `blocked_codes`：稳定的配置阻断原因码，与 `blocked_by` 的说明对应。
- `diagnostics`：`ready` 为账号在线且配置检查通过；`checks` 包含 `code/title/passed/message/target`，通过项的 `message` 为该项失败时的说明，仅失败时展示；`note` 说明检查范围。

诊断不会请求模型、执行工具或发送 QQ 消息，不验证群成员关系、具体消息触发、用户权限、冷却与实际调用结果。`configured` 继续只表示配置就绪，不包含连接状态。群覆盖仍由群内所有 Bot 共享；指定 `self_id` 只改变查看视角，不修改配置。`self_id` 必须为正整数。

`GET /api/console/readiness` 的 `responding_groups` 按已知机器人的默认配置计算；新增 `ready_groups` 同时检查账号在线状态。两者都只检查已登记群，不保证账号已加入该群或真实调用成功。

以下接口使用与其他管理 API 相同的管理令牌：

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/api/console/readiness` | GET | 账号、模型、全局响应开关、实际可响应的已登记群、失败插件 |
| `/api/console/sessions` | GET | 已保存 AI 会话目录（场景、会话 ID、消息数、更新时间） |
| `/api/console/groups/{group_id}` | GET | 单群原始开关与实际生效状态，白黑名单和模型覆盖 |
| `/api/console/groups/{group_id}` | PATCH | 一次保存单群 `enabled`、`llm_enabled` 和 `persona` |

单群 PATCH 的 `enabled` 必填；`llm_enabled: null` 和 `persona: ""` 移除对应覆盖。全局模型开关与群黑白名单仍优先，响应同时返回生效状态。保存保留其他单群配置，并在一次数据库更新中持久化。

`GET /api/config` 的 `config` 和 `overrides` 均隐藏模型、管理和连接密钥。

单群 PATCH 支持可选 `policy` 对象，包含 `name`、`auto_group`、`trigger_mode`（`at/prefix/all`）、`trigger_prefix`、`cooldown_seconds`（0–120）、`allowed_tools`、`provider` 和 `system_extra`。`allowed_tools` 是工具 ID 列表（形如 `插件名__工具名`，例如 `web__get_weather`），可在 `GET /api/plugins` 的每个插件 `ai_tools[].name` 中查看。空字段继承原有配置，空工具列表明确表示禁用所有工具。传 `policy: null` 移除独立策略；省略 `policy` 保持原策略。设置与策略在同一次数据库更新中保存。

`policy.auto_reply_probability`（0–100）控制“自动参与”模式下普通消息交给智能体处理的概率，默认 100；被 @ 或唤醒词触发时不应用概率筛选。

GET/PATCH 响应增加 `policy`、`inherited_policy` 和 `effective_policy`。生效策略包含实际模型、人格、可用工具 `tools`（含 `id/title/plugin/description`）、不可用工具 `unavailable_tools` 以及 `blocked_by` 原因，便于管理端解释为何未响应。

基础地址：`http://<server.host>:<server.port>`（默认 `http://127.0.0.1:6701`）。

- 交互式文档：`GET /api/docs`（Swagger UI）
- OpenAPI：`GET /api/openapi.json`

## 鉴权

除 `/api/health`、`/api/version` 外都需要令牌。建议使用请求头；`?token=` / `?access_token=` 为旧客户端兼容方式，令牌会出现在请求 URL 中，不要将这种链接用于日志、聊天或外部跳转：

```http
Authorization: Bearer <token>
X-Admin-Token: <token>
GET /api/status?token=<token>
```

控制台 `/ui?token=<token>` 仅用于首次引导；页面缓存令牌后立即清除地址栏查询参数。HTML 同时设置 `no-store`、`no-cache` 和 `Referrer-Policy: no-referrer`，避免缓存页面或由页面转发完整来源 URL。

令牌来源：`config.toml` 的 `admin.token`；留空时首次启动自动生成到 `data/admin_token.txt`
（启动日志里也会打印一次）。

```powershell
$token = Get-Content data\admin_token.txt
Invoke-RestMethod -Headers @{ Authorization = "Bearer $token" } http://127.0.0.1:6701/api/status
```

未鉴权返回 `401 {"detail":"管理令牌无效"}`。

## 端点

### 基础

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/health` | 否 | `{ok, version, started, connections, uptime_seconds}`，适合做探活 |
| GET | `/api/version` | 否 | 名称与版本 |
| GET | `/api/status` | 是 | 聚合状态：连接、插件、分发统计、存储、LLM、人格 |
| GET | `/api/connections` | 是 | 当前 NapCat 连接列表（`self_id`、online、事件/调用计数、空闲秒数） |
| POST | `/api/storage/backup` | 是 | 创建 SQLite 在线一致性备份；返回备份路径、schema 版本、表数和大小 |

### 插件

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/plugins` | 插件列表：元信息、处理器、是否启用、是否内置、来源路径 |
| GET | `/api/plugins/{name}` | 单个插件 |
| POST | `/api/plugins/reload` | body `{"name": "basic"}` 指定重载；空 body 重载全部 |
| POST | `/api/plugins/{name}/reload` | 重载指定插件 |
| POST | `/api/plugins/{name}/enabled` | body `{"enabled": false, "group_id": 123}`；`group_id` 省略表示全局 |
| GET | `/api/plugins/{name}/settings` | 插件配置：`{"schema": [...], "values": {...}}`，可带 `?group_id=` 取群级生效值 |
| PATCH | `/api/plugins/{name}/settings` | body `{"values": {...}, "group_id": 123?}`；校验失败 `400` 且整体不生效 |

插件摘要新增 `package`（是否目录包）、`manifest`（是否包含标准包头）与 `folder`（文件夹名）。
`name`、处理器、工具、配置和权限接口保持兼容。标准目录与离线开发命令见 [插件包规范](plugin-packages.md)。

### 工具

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/tools` | 工具目录：`id`（`插件名__工具名`）、`title`、说明、权限、`effect`、`risk`、`capabilities`、来源插件及其启用状态、`globally_allowed`、`group_count`；`registry` 返回快照版本、工具数、重建/复用次数 |
| GET | `/api/sandbox` | 沙盒策略摘要、`workspace_scope`、能力开关、主机/大小/额度限制、允许/拒绝计数与最近 20 条裁决元数据；`?group_id=` 预览单群覆盖与该群实际工作区路径 |

工具由插件通过 `on_ai_tool` 声明，属于插件能力而不是插件本身；`allowed_tools` 保存的是工具 `id`。
`registry` 是宿主工具目录快照信息：`revision` 在插件工具变化后递增，`rebuilds` / `reused` 分别表示重建与
复用次数，`tools` 为登记工具数。目录契约见 [Agent 与插件](tool-registry.md)。
`capabilities` 是 `fs.read`、`fs.write`、`net.read`、`net.write`、`email.send`、`qq.send`、`qq.manage`、`memory.read`、
`system.read`、`qq.send_file`、`screen.capture`、`process.exec` 的子集，`risk` 为 `read|write|destructive`。执行前由 `[sandbox]` 策略裁决；
被拒绝时工具结果返回 `{"error": "tool_blocked", "reason": ...}`，不产生外部动作。设计见
[群聊智能体与执行沙盒](agent-sandbox.md)。

插件配置的 schema 由插件用 `plugin.setting(...)` 声明，字段含
`key` / `type` / `label` / `default` / `description` / `options` / `min` / `max` / `secret` / `group_scoped`，
桌面端可以据此直接渲染表单。`secret=true` 的字段返回值会打码成 `******`。

插件配置端点仅适用于声明了配置项的已加载插件。`agent` 的参数在 WebUI 左侧「Agent」独立页面配置；群聊响应默认关闭，需为指定群开启并在「允许调用的工具」里勾选工具后，该群消息才会交给模型。

### 群

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/groups` | 已登记群及开关、群配置 |
| POST | `/api/groups/{group_id}/enabled` | body `{"enabled": true}` |
| GET | `/api/groups/{group_id}/config` | 群配置（`llm.enabled`、`llm.persona` 等） |
| PATCH | `/api/groups/{group_id}/config` | 深合并更新群配置 |

### 配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/config` | `{"config": 生效配置（密钥已隐藏）, "overrides": 运行时覆盖}` |
| PATCH | `/api/config` | 深合并 + 持久化，例如 `{"llm": {"model": "deepseek-chat"}}` |

生效优先级：运行时覆盖（数据库） > 环境变量 > `config.toml` > 默认值。
校验失败返回 `400`，且不会部分生效。

### 日志与统计

| 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/logs` | `limit`(≤2000) `level` `since` | 内存环形缓冲里的最近日志 |
| GET | `/api/stats` | `days`(1~90) | 总消息数、每日消息、活跃用户、会话房间、分发统计、LLM 状态 |

### 人格与 LLM

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/personas` | 人格列表（`name`、`title`、`description`、`preview`、`editable`）；全部由用户创建 |
| GET | `/api/personas/variables` | 人格提示词可用变量（`name`、`description`、`example`），供管理端「说明」使用 |
| GET | `/api/personas/{name}` | 含完整内容 |
| PUT | `/api/personas/{name}` | 创建或更新用户人格；body 为 `title`、`description`、`content` |
| DELETE | `/api/personas/{name}` | 删除用户人格；若它是当前默认人格，同时清空默认人格并返回新的 `default` |
| POST | `/api/personas/reload` | 重新扫描 `personas/` |
| POST | `/api/llm/test` | body `{"prompt": "...", "persona": "demo", "provider": "deepseek"}`，单轮直连 |
| POST | `/api/llm/chat` | body `{"message","scene","scene_id","persona","include_history","save","provider"}`，走上下文 |
| GET | `/api/llm/providers` | 各 provider 的 model / base_url / 是否已配置（不返回密钥） |
| GET | `/api/llm/models?provider=` | 透传上游 `/models` |
| GET | `/api/llm/usage?range=today\|7d\|30d\|all` | 调用次数、失败数、token、平均延迟、费用、按模型分组 |

`provider` 省略时按 `llm.routes` / `llm.default` 路由。每次调用都会写入 `llm_usage` 表，
失败记录会带上错误摘要。

LLM 未启用或没配 Key 时返回 `400`；上游报错返回 `502`（含错误摘要）。

### 会话历史

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/chat/{scene}/{scene_id}` | `limit` 默认 50；`scene` 为 `group` / `private`；每条含 `role`、`content`，用户消息另带 `user_id`、`nickname`、`sender_role` |
| DELETE | `/api/chat/{scene}/{scene_id}` | 仅接受正数编号及 `group` / `private` 场景；清空并返回删除条数，与同库管理审计同事务提交，不记录消息内容 |

### 通用调用与 KV

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/actions` | body `{"action":"get_login_info","params":{},"self_id":10001}`，透传 OneBot API；无连接时 `503` |
| GET | `/api/kv` | 所有 scope |
| GET | `/api/kv/{scope}` | scope 下所有键值 |
| PUT | `/api/kv/{scope}/{key}` | body `{"value": 任意 JSON}`；保存与脱敏作用域审计同事务提交 |
| DELETE | `/api/kv/{scope}/{key}` | 删除；仅实际删除时写入作用域审计 |

### 实时事件流（SSE）

```
GET /api/events/stream?token=<token>
```

`EventSource` 不能带请求头，所以令牌走查询参数。输出：

```
: connected

data: {"type":"event","kind":"message","message_type":"group","group_id":20002,"user_id":30003,"text":"/ping", ...}

data: {"type":"connection","online":true,"self_id":10001}

: ping
```

每 15 秒无数据会发一次 `: ping` 保活；订阅队列满时丢弃最旧事件，不会阻塞机器人。

```javascript
const es = new EventSource(`http://127.0.0.1:6701/api/events/stream?token=${token}`);
es.onmessage = (event) => console.log(JSON.parse(event.data));
```

### 状态响应示例

```json
{
  "version": "0.1.0",
  "started": true,
  "uptime_seconds": 128.4,
  "connections": [
    {"self_id": 10001, "online": true, "events_received": 52, "actions_sent": 7, "idle_seconds": 3.1}
  ],
  "plugins": [
    {"name": "ping", "version": "0.1.0", "enabled": true, "builtin": true, "handlers": [...]}
  ],
  "dispatcher": {"handlers": {"message": 5, "notice": 2}, "stats": {"message": 52}},
  "tools": {"revision": 3, "tools": 32, "plugins": ["basic", "workspace"], "rebuilds": 3, "reused": 128},
  "sandbox": {"enabled": true, "stats": {"allowed": 4, "denied": 1, "denied:automatic_side_effect": 1}},
  "storage": {"path": "data/fllbot.db", "size_bytes": 40960, "schema_version": 1, "messages": 52},
  "llm": {"enabled": true, "available": true, "model": "deepseek-chat", "requests": 3, "failures": 0}
}
```

## 错误约定

| 状态码 | 含义 |
| --- | --- |
| 400 | 参数/配置无效 |
| 401 | 令牌缺失或错误 |
| 404 | 插件、人格不存在 |
| 502 | 上游（NapCat / 模型服务）调用失败 |
| 503 | 当前没有在线的 NapCat 连接 |

工具被沙盒拒绝不是 HTTP 错误：工具结果里是 `tool_blocked` 与原因码；管理接口只记录元数据。
群策略新增 `participation_threshold`（自动参与评分阈值）与 `sandbox_write`、
`sandbox_network`、`sandbox_private_network`、`sandbox_email`（单群沙盒覆盖，`null` 跟随全局）。


### 机器人与昵称唤醒

侧栏“机器人”展示已连接过的账号、昵称及在线状态。账号连接时自动通过 OneBot `get_login_info` 同步昵称，也可点击“同步账号与群聊”刷新。昵称暂时获取失败时保留该账号上次成功的昵称；未设置自定义唤醒词且从未获取昵称的账号不响应唤醒。

每个 Bot 可分别保存群聊默认回复方式、模型和人格。优先级为：单群覆盖 > Bot 默认设置 > 系统默认设置；全局模型、权限和访问范围开关仍有效。同一群的覆盖对群内所有 Bot 生效，未覆盖项目分别继承各自 Bot 默认值。

“听到唤醒词时回复”使用该 Bot 的自定义 `wake_word`，留空时使用账号昵称。例如设置为“小助手”，发送“小助手，你好”即可。只发送唤醒词（不带其他内容）时也会自然回应，不会静默忽略。自定义唤醒词不会被昵称同步覆盖，清空后恢复跟随昵称。旧群 `trigger_prefix` 兼容保留但不再用于匹配。

接口：`GET /api/console/bots`，`POST /api/console/bots/refresh`，`PUT /api/console/bots/{self_id}/defaults`（`reply_mode: inherit|at|prefix|all|off`、`wake_word`（最多 80 字，留空使用昵称）、`provider`、`persona`）。都需要管理令牌。群详情接口支持 `?self_id=` 查看指定 Bot 的实际继承结果。


群聊配置表使用 `GET /api/console/groups?group_ids=123&group_ids=456` 批量读取详情，需要管理令牌；每次最多 200 个正整数群号，重复群号去重。每行修改仍通过原有单群 PATCH 保存，未提交的 policy 保留。


单群配置的 `has_override` 表示已主动创建或存在旧版覆盖。`POST /api/console/groups/{group_id}/rule` 主动新增一条配置，幂等且不改变原有行为；`DELETE /api/console/groups/{group_id}/rule` 移除覆盖并恢复默认开关、模型、人格和智能体设置，准入恢复默认，保留其他群名单与其他插件配置。读取群列表不会新增覆盖记录。


访问规则合并在配置表：`GET/PATCH /api/console/access` 读取和保存默认 `mode: all|listed`，`PATCH /api/console/groups/{group_id}/access` 设置该群 `access: inherit|allow|deny`。旧 `allow_groups`/`deny_groups` 自动兼容；新 `permission.group_access` 默认为 `auto`（沿用旧名单语义），显式 `listed` 支持空白名单拒绝所有未准入群。移除一条配置也移除该群名单记录，保留其他群，移除最后一个准入群不会自动开放全体。

批量准入通过 `PATCH /api/console/groups/batch-access` 保存，提交 `group_ids`（1–200 个正整数）和 `access`；只更新选中群，名单在一次配置更新中保存。


单群“回复方式”支持同时勾选“被 @”“唤醒词”“自动参与”，点击“应用”后保存，任意一个条件满足即可响应；同时命中多个条件也只处理一次。“跟随默认”和“关闭 AI”与其他选项互斥。API 的 `policy.trigger_modes` 为非空列表（`at`、`prefix`、`all`），旧 `trigger_mode` 单选字段继续兼容；修改其他详细设置会保留已选组合。


扩展摘要包含 `category: plugin|tool`。插件列表包含全部插件；`GET /api/tools` 的 `providers` 返回至少注册了一个 Agent 工具的插件（供工具项显示来源并打开插件配置）。`PATCH /api/tools/{tool_id}/enabled` 通过 `enabled` 逐项设置默认 Agent 工具授权，不连带修改其他工具或插件开关，需管理令牌。


### 模型能力诊断与可靠性设置

`POST /api/llm/diagnose` 需要管理令牌。请求体 `{"provider":"default"}`，省略 provider 使用默认服务。
返回 `provider`、`model`、`ok` 及 `checks`，每项含 `kind`（chat/tools/images）、`status`（passed/failed/skipped）、`detail`、`latency_ms`。
诊断只调用被测模型，不使用备用服务、不执行插件工具或向 QQ 发送内容；用量记入 diagnostic 场景。

`GET /api/llm/providers` 增加模型能力字段、上下文窗口、输出预算、并发和单价；不返回密钥。
用 `PATCH /api/config` 的 llm 段或 llm.providers.<名称> 保存能力与单价。
全局 llm 段支持 `fallback_providers`、`group_daily_token_limit`、`group_daily_cost_limit`（0 为不限）。
对话结果增加 `finish_reason` 和 `truncated`；达到输出上限会附带截断提示，截断的工具调用不会执行。
完整参数、预算与费用限制边界见 [LLM 子系统](llm.md)。

### 数据保留与清理

以下接口均需管理令牌：

| 接口 | 用途 |
| --- | --- |
| `GET /api/console/data-retention` | 策略、配置版本和清理状态 |
| `PUT /api/console/data-retention` | 保存 `enabled`、`interval_seconds`、`messages_days`、`conversations_days`、`usage_days`、`changes_days`；支持 `If-Match`，冲突 409 |
| `POST /api/console/data-cleanup/preview` | 只读预览，返回各表数量、总数、范围、`id`、`expires_at`、`can_execute` |
| `POST /api/console/data-cleanup` | 提交 `{"preview_id":"预览 id","confirm":true}`，202 返回作业 |
| `GET /api/console/data-cleanup/jobs/{id}` | 查询 pending/running/completed/conflict/failed；重启后可能 404 |
| `GET /api/console/data-cleanup/runs?before=123&limit=20` | 持久化成功清理结果，游标分页，最多每页 100 条 |

预览请求：`datasets` 为 messages/conversations/usage/traces/changes 中的非空列表，`older_than_days` 为 1–3650，可选 `scene: group|private`、正整数 `scene_id` 和 `self_id`。指定会话号必须指定场景；只有 messages/traces 支持按 Bot 过滤，其余数据为共享范围。管理变更没有私聊归属，因此私聊范围选择 `changes` 时计数为 0，不会清除无关全局审计。返回固定截止时间，执行时不重新推进截止时间。预览最多保留 20 个，有效 10 分钟，单次最多 50000 条；记录集合变化时作业 conflict，事务不删除数据，需重新预览。提交成功后预览只能使用一次。

自动清理默认关闭；各类天数 0 表示不自动清理，间隔 60–86400 秒。启用后按间隔检查，每类每轮最多处理 5000 条原始记录，相关摘要同步失效，总量仍受 50000 条保护。保存不立即执行；重启后重新等待间隔。消息诊断另有固定 7 天/10000 条上限，可手动提前清理。

会话清理覆盖旧 AI 历史、群记忆原文和关联摘要（包括仍引用已删除原文的新摘要）；不会删除配置、插件 KV、任务、备份及日志文件。成功删除才写入清理结果，最多保留 1000 条元数据，不含原文。用量/消息统计随保留记录减少；至少保留最近一天，避免清除当日额度依据。数据库旧备份可能仍含已清理数据。

### 人格文件版本

`GET /api/personas` 和 `GET /api/personas/{name}` 会在共享设置锁内重新扫描人格目录。人格摘要和保存响应包含带引号的 SHA-256 `revision`，详情及保存响应同时返回 `ETag`。详情中的内容和版本取自同一次文件读取。

编辑 `PUT /api/personas/{name}`、删除 `DELETE /api/personas/{name}` 可携带详情返回的 `If-Match`；提交前重新读取文件，版本不同或文件已删除返回 409。创建时发送 `If-None-Match: *`，同名已存在则 409；两个条件头不能同时使用，其他 If-None-Match 值返回 400。旧客户端不带条件头仍兼容，属于无条件写入。控制台均使用条件请求，冲突保留草稿。

人格保存先在同目录写完整临时文件再替换原文件，失败保留旧文件和运行态；`.txt` 和自定义文件名保持原路径。目录说明/示例保留名称不能创建人格。共享设置锁保护实例内管理写入；磁盘变更在检查时被识别，但不提供与任意外部编辑进程之间的跨进程文件事务。

人格保存、删除和重新扫描在管理变更记录中写入开始与结果事件，仅记录人格标识和结果，不记录提示词正文。删除系统默认人格时先持久化清空默认引用，再删除文件；文件删除失败会尝试恢复原默认值。恢复也失败时仍保持默认值为空（安全回退），并单独记录恢复失败。跨数据库与文件系统不具备共同事务，开始事件用于识别进程中断留下的未完成操作。

插件手动重载记录开始及每个插件的成功/失败结果，不记录异常正文。批量重载会继续处理其他插件；响应包含 `reloaded`、`failed` 列表，部分失败时 `ok` 为 false。

### 插件任务中心

全部接口需要管理令牌：

| 接口 | 用途 |
| --- | --- |
| `GET /api/console/task-sources` | 当前已加载插件声明的来源，含 id、title、plugin、enabled |
| `GET /api/console/tasks/{source_id}` | 按来源分页筛选任务 |
| `GET /api/console/tasks/{source_id}/{task_id}/runs` | 该任务的持久化运行记录 |
| `POST /api/console/tasks/{source_id}/{task_id}/actions` | 执行带版本的管理操作 |

来源 ID 格式如 `daily__reminders`。任务列表支持 `before` 正整数游标、`limit` 1–100（默认 20）、`self_id`、`scene: group|private`、`scene_id`、`owner` 和 `state: pending|paused|completed|cancelled`；指定 scene_id 时必须指定 scene。运行记录支持 before/limit。两类分页均返回 items/next_cursor。插件未加载、任务不存在返回 404；版本或状态冲突返回 409。停用的已加载插件仍可查看与管理任务，但实际发送必须通过权限检查。

管理请求示例：`{"action":"pause","revision":"1","acknowledge_duplicate":false}`。action 可选 pause/resume/cancel/complete/retry，实际允许操作由任务返回的 actions 决定。daily 的 retry 只适用于 pending 且 delivery=uncertain 的任务，必须 acknowledge_duplicate=true；成功仅重新入队，不立即发送。sending 状态不能取消或重试，sent 不能重发；完成/取消不删除历史或撤回 QQ 消息。

任务响应包含 id、revision、content、self_id、scene、scene_id、owner、due_at（Unix 秒或 null）、timezone、state、delivery、last_code、actions。daily 的 delivery 为 waiting/sending/sent/uncertain。运行记录包含 id、task_id、status、code、created_at、finished_at，不保存正文、错误原文或凭据。记录总数上限 10000，旧版已送达任务不补造运行历史。

### 批量替换共享资源引用

`GET /api/console/resources/{kind}/{name}/references` 的响应增加 alternatives（其他可选资源的 name/title）。以下写接口均需管理令牌，kind 为 persona/provider：

- `POST /api/console/resources/{kind}/{name}/replacement-preview`，请求 `{"target":"新资源标识"}`。只读预览，返回 id、kind、source、target、expires_at、count、items、truncated、can_execute 和说明。单次最多 1000 处直接引用，预览有效 10 分钟，实例最多保留 20 个预览。
- `POST /api/console/resources/replacements`，请求 `{"preview_id":"预览 id","confirm":true}`。确认整体替换，返回 count、change_id 和说明。预览只能提交一次；缺失、过期、资源不存在或任意配置版本变化返回 409，重新预览后再提交。不存在引用或超出数量上限不执行。

替换覆盖系统默认、模型路由、备用列表、Bot 默认值和群的显式绑定。备用列表替换后按原顺序去重；继承场景随上层值生效，不重复计算为直接引用。操作保留原资源、凭据、群开关、其他配置、历史和插件数据；未指定服务时的隐式首个模型回退不作为显式引用替换。原资源可在确认无引用后通过已有入口另外删除，模型目前没有删除入口。

执行在共享设置锁内重新核对全局配置、全部 Bot/群配置和人格文件版本；事务中再次核对数据库快照，再原子提交配置覆盖、Bot 资料和群设置。任意 SQL 写入失败整体回滚，提交成功才同步发布运行态。浏览器取消请求也不会打断已开始的事务与运行态发布。结果可凭 change_id 在 management_changes 中找到 resource_replaced 元数据；不保存密钥或提示词内容。正在处理的模型请求可能仍使用旧资源，页面中的其他配置草稿不会自动更新，保存时仍受其原版本检查保护。

### 插件配置原子保存与版本

`GET /api/plugins/{name}/settings?group_id=123` 与 PATCH 响应增加 revision/ETag。PATCH 可携带 If-Match，过期返回 409；控制台自动携带，冲突保留草稿。版本包含字段 schema、实际生效值、全局/群原值；Agent 群配置还包含对应群配置版本，避免群策略更新后用旧视图保存。旧调用不带 If-Match 仍兼容无条件写入。

全部字段校验后，在一个独立数据库事务提交全局和群项以及字段级审计；任意写入失败整体回滚，不提前发布缓存。请求取消会等待已开始事务及缓存发布完成。values 和 effective 均为遮盖后的响应，schema.secret 的非空默认值同样遮盖。提交 `******` 到密钥字段表示保留，明确提交空字符串表示清空；控制台留空仍采用保持原密钥的既有语义。群号必须为正整数，未提供则为全局。群请求中的非群级字段仍落到全局，控制台禁用这些字段，外部客户端须依据 schema.group_scoped 判断影响范围。

### 账号默认值版本与编辑

`GET /api/console/bots` 和昵称同步响应的每个账号增加 revision，版本只包含该账号的默认值，昵称变化不使其失效。`GET /api/console/bots/{self_id}/defaults` 需要管理令牌，返回 self_id、nickname、online、defaults、revision、providers、personas、global_wake_word 和优先级说明，ETag 为 revision。只允许已知账号（在线连接或持久化资料），未连接过返回 404；离线资料仍可编辑。

`PUT /api/console/bots/{self_id}/defaults` 支持 If-Match，过期 409 并保留客户端草稿；旧客户端无条件写入仍兼容。请求为 BotDefaults 完整对象，省略字段使用代码默认值；空资源/唤醒词跟随上层或昵称，reply_mode=inherit 跟随实例默认，off 仅关闭此 Bot 的默认 AI 回复，群单独配置仍可覆盖。模型和人格必须存在。返回 defaults/revision/ETag。

账号资料与 bot_profile_updated 字段级审计同事务保存，写入失败整体回滚；提交成功才发布缓存。请求取消仍会完成已开始事务及发布。审计保留账号 ID 和改动字段，不记录昵称、唤醒词或提示词的值。昵称同步只更新身份资料，不覆盖默认值；读取资料返回独立副本。共享资源替换导致的默认值变更同样改变版本，旧窗口保存会冲突。

### 启动参数与管理入口

FLLBot 构造时固定启动字段：server.host/port/cors_origins、admin.enabled/token_file、plugin.dirs/builtin/hot_reload/watch_interval/max_concurrency/max_pending_events、storage.path、logging.dir/file/json_lines/ring_size。`PATCH /api/config` 或服务层更新尝试改变这些值时，整次请求不保存，HTTP 409 detail 为 `{code: startup_config_read_only, fields: [...], message: ...}`。同值提交允许但不保存为运行时覆盖。其他动态字段继续在线更新；server.access_token/call_timeout 对新连接生效，heartbeat_timeout 动态更新，backup_dir 后续备份操作读取。

旧 config_overrides 中的启动字段在启动时以单事务移至 meta.inactive_startup_overrides，并记录 startup_overrides_archived 字段元数据；活动覆盖移除这些字段，历史原值保留，不参与生效。归档失败整体回滚并中止启动，不丢原值；重复启动不重复归档。GET /api/config 增加 startup.active（启动配置值）、startup.inactive_overrides（旧覆盖历史）及说明。

admin.enabled=false 只能通过启动配置设置。关闭后后台页面、管理 API（包括事件流）、API 文档/OpenAPI 均返回 404，保留 /api/health、/api/version 和 OneBot WebSocket。管理入口关闭时不会自动创建管理令牌文件。重新启用须修改启动来源并重启。

配置校验错误不回显合并配置的 input_value，避免带出服务器密钥；持久化失败返回通用 500 且不发布候选配置。已开始的全局配置提交受请求取消保护，提交后同步发布运行态。

### 插件开关硬限制与版本

`GET /api/plugins/{name}/enabled?group_id=123` 需要管理令牌，返回 enabled（最终插件开关值）、global_state、group_state、default_enabled、blocked_by_global、source、loop_enabled、revision/ETag 和说明。省略 group_id 查看实例默认状态。其他场景/用户权限仍单独检查，enabled 并不代表该群必然可以处理消息。

`POST /api/plugins/{name}/enabled` 保留请求 `{enabled: bool, group_id?: 正整数}`，支持 If-Match，过期 409。响应原 enabled 字段仍表示请求设置值，effective 为实际生效状态；全局停用时给某群配置 enabled=true 会保存覆盖，但 effective.enabled=false。插件摘要增加 revision 与 blocked_by_global，控制台开关自动携带版本。旧客户端不带版本仍兼容。

全局明确 false 为硬限制；群 true 无法绕过，群原值不被删除。全局未明确停用且插件声明默认 false 时，群 true 仍可启用。周期回调仅在实例默认启用或至少一群单独启用时调度，全局 false 阻止新一轮回调；回调仍负责检查具体目标权限，生命周期初始化及管理读取不因此卸载。已进入执行的回调/外部请求不保证中止或撤回。消息与工具在调用前检查同一插件权限。

开关持久化和 plugin_state_updated 字段审计同事务提交，失败整体回滚；请求取消仍发布已提交状态。同值重复提交不增加审计。记录只含插件及字段名，不复制业务内容。

工具调用不限制轮数或单轮工具数量；旧版 `max_tool_rounds` 字段仅保留输入兼容性，不再影响执行。
