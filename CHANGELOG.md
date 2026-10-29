# 更新记录

## v0.2.0

### 新增

- **插件声明式配置**：`plugin.setting(...)` 声明 bool/int/float/string/text/select/multiselect/list 字段，
  支持默认值、范围校验、敏感值打码、`group_scoped` 按群覆盖；新增
  `GET/PATCH /api/plugins/{name}/settings`，桌面端可直接渲染表单。
- **`group_guard` 插件**：入群欢迎/退群提示（模板占位符）、违禁词（支持 `re:` 正则）、
  刷屏检测，可配置撤回 / 禁言 / 仅警告；命令 `/guard`、`/welcome`、`/badword`。
- **`reminder` 插件**：`/remind 10m 喝水`、列表、删除；SQLite 持久化（迁移 v2），断线后恢复继续触发。
- **`admin` 插件**：`/status`、`/reload <插件>`、`/plugin on|off <插件>`、`/group on|off`。
- **事件去重**：同一 `self_id + message_id` 在 3 分钟内重复上报只处理一次，避免 NapCat 重连重放。
- **启动自检**：`fllbot run` 先探测端口占用，给出明确提示而不是 uvicorn 报错。

### 修复

- 并发首次登记新群时可能触发 `UNIQUE constraint failed: groups.group_id`（改为原子 upsert + 加载锁）。
- 群通知事件里 `ctx.is_group` 恒为假，导致欢迎/退群消息发不出去。

### 变更

- `Storage.SCHEMA_VERSION` 升到 2，启动时自动迁移，老数据库无需手工处理。
- 版本号统一到 0.2.0。

## v0.1.0

首个版本：OneBot 11 反向 WebSocket、插件系统与热重载、权限与 SQLite、LLM 多轮对话、
管理 REST API 与 SSE、内置 `ping` / `echo` / `help` / `llm_chat` / `request_notify`。
