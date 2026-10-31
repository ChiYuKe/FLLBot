# 管理 API

## 控制台接口

以下接口使用与其他管理 API 相同的管理令牌：

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/api/console/readiness` | GET | 账号、模型、全局响应开关、实际可响应的已登记群、失败插件 |
| `/api/console/sessions` | GET | 已保存 AI 会话目录（场景、会话 ID、消息数、更新时间） |
| `/api/console/groups/{group_id}` | GET | 单群原始开关与实际生效状态，白黑名单和模型覆盖 |
| `/api/console/groups/{group_id}` | PATCH | 一次保存单群 `enabled`、`llm_enabled` 和 `persona` |

单群 PATCH 的 `enabled` 必填；`llm_enabled: null` 和 `persona: ""` 移除对应覆盖。全局模型开关与群黑白名单仍优先，响应同时返回生效状态。保存保留其他单群配置，并在一次数据库更新中持久化。

`GET /api/config` 的 `config` 和 `overrides` 均隐藏模型、管理和连接密钥。

单群 PATCH 支持可选 `policy` 对象，包含 `name`、`auto_group`、`trigger_mode`（`at/prefix/all`）、`trigger_prefix`、`cooldown_seconds`（0–120）、`max_tool_rounds`（1–5）、`allowed_tools`、`provider` 和 `system_extra`。`allowed_tools` 是工具 ID 列表（形如 `插件名__工具名`，例如 `web__get_weather`），可在 `GET /api/plugins` 的每个插件 `ai_tools[].name` 中查看。空字段继承原有配置，空工具列表明确表示禁用所有工具。传 `policy: null` 移除独立策略；省略 `policy` 保持原策略。设置与策略在同一次数据库更新中保存。

GET/PATCH 响应增加 `policy`、`inherited_policy` 和 `effective_policy`。生效策略包含实际模型、人格、可用工具 `tools`（含 `id/title/plugin/description`）、不可用工具 `unavailable_tools` 以及 `blocked_by` 原因，便于管理端解释为何未响应。

基础地址：`http://<server.host>:<server.port>`（默认 `http://127.0.0.1:6701`）。

- 交互式文档：`GET /api/docs`（Swagger UI）
- OpenAPI：`GET /api/openapi.json`

## 鉴权

除 `/api/health`、`/api/version` 外都需要令牌，支持三种传法：

```http
Authorization: Bearer <token>
X-Admin-Token: <token>
GET /api/status?token=<token>
```

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
| GET | `/api/tools` | 工具目录：`id`（`插件名__工具名`）、`title`、说明、权限、来源插件及其启用状态、`globally_allowed`、`group_count` |

工具由插件通过 `on_ai_tool` 声明，属于插件能力而不是插件本身；`allowed_tools` 保存的是工具 `id`。

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
| GET | `/api/personas` | 人格列表（`name`、`title`、`description`、`preview`、`editable`） |
| GET | `/api/personas/{name}` | 含完整内容 |
| PUT | `/api/personas/{name}` | 创建或更新用户人格；body 为 `title`、`description`、`content` |
| DELETE | `/api/personas/{name}` | 删除用户人格；内置人格和当前默认人格不可删除 |
| POST | `/api/personas/reload` | 重新扫描 `personas/` |
| POST | `/api/llm/test` | body `{"prompt": "...", "persona": "fllbot", "provider": "deepseek"}`，单轮直连 |
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
| DELETE | `/api/chat/{scene}/{scene_id}` | 清空并返回删除条数 |

### 通用调用与 KV

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/actions` | body `{"action":"get_login_info","params":{},"self_id":10001}`，透传 OneBot API；无连接时 `503` |
| GET | `/api/kv` | 所有 scope |
| GET | `/api/kv/{scope}` | scope 下所有键值 |
| PUT | `/api/kv/{scope}/{key}` | body `{"value": 任意 JSON}` |
| DELETE | `/api/kv/{scope}/{key}` | 删除 |

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
