# 管理 API

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
| POST | `/api/plugins/reload` | body `{"name": "ping"}` 指定重载；空 body 重载全部 |
| POST | `/api/plugins/{name}/reload` | 重载指定插件 |
| POST | `/api/plugins/{name}/enabled` | body `{"enabled": false, "group_id": 123}`；`group_id` 省略表示全局 |
| GET | `/api/plugins/{name}/settings` | 插件配置：`{"schema": [...], "values": {...}}`，可带 `?group_id=` 取群级生效值 |
| PATCH | `/api/plugins/{name}/settings` | body `{"values": {...}, "group_id": 123?}`；校验失败 `400` 且整体不生效 |

插件配置的 schema 由插件用 `plugin.setting(...)` 声明，字段含
`key` / `type` / `label` / `default` / `description` / `options` / `min` / `max` / `secret` / `group_scoped`，
桌面端可以据此直接渲染表单。`secret=true` 的字段返回值会打码成 `******`。

```powershell
# 查看 group_guard 的配置 schema
Invoke-RestMethod -Headers @{Authorization="Bearer $token"} `
  http://127.0.0.1:6701/api/plugins/group_guard/settings

# 全局修改违禁词
Invoke-RestMethod -Method Patch -Headers @{Authorization="Bearer $token"} -ContentType "application/json" `
  -Uri http://127.0.0.1:6701/api/plugins/group_guard/settings `
  -Body '{"values": {"badwords": ["广告", "re:加\\s*群"]}}'

# 只给 20002 群设置专属欢迎语
Invoke-RestMethod -Method Patch -Headers @{Authorization="Bearer $token"} -ContentType "application/json" `
  -Uri http://127.0.0.1:6701/api/plugins/group_guard/settings `
  -Body '{"group_id": 20002, "values": {"welcome_template": "本群专属欢迎 {nickname}"}}'
```

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
| GET | `/api/personas` | 人格列表（`name`、`title`、`description`、`preview`） |
| GET | `/api/personas/{name}` | 含完整内容 |
| POST | `/api/personas/reload` | 重新扫描 `personas/` |
| POST | `/api/llm/test` | body `{"prompt": "...", "persona": "fllbot"}`，单轮直连 |
| POST | `/api/llm/chat` | body `{"message","scene","scene_id","persona","include_history","save"}`，走上下文 |

LLM 未启用或没配 Key 时返回 `400`；上游报错返回 `502`（含错误摘要）。

### 会话历史

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/chat/{scene}/{scene_id}` | `limit` 默认 50；`scene` 为 `group` / `private` |
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
