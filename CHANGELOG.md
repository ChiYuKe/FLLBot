# 更新记录

## 未发布

### 新增

- **标准插件包框架**：voice、web 与内置 basic、agent 按文件夹独立封装，统一 plugin.toml 包头、入口、README、更新记录与开发文档；保留原有名称、配置与权限。新增 `plugin new/check/list` 开发命令、资源清理、重载失败回滚和架构检查。

- **群聊发送者标注**：会话历史会记录每条用户消息的发送者昵称、QQ 号与群身份（群主 / 管理员 /
  成员），智能体看到的历史格式为 `昵称(QQ号，身份)：内容`，可以区分不同群员的发言。
- **`web` 插件**：网页搜索（必应免密钥，可切 Tavily / Serper / SearXNG）、读取公开网页正文、城市天气
  （Open-Meteo，免密钥）和当前时间；提供 `/搜索`、`/天气`、`/现在` 命令，以及
  `web__web_search` / `web__read_webpage` / `web__get_weather` / `web__get_current_time` 四个智能体工具。
  智能体需要在「允许调用的工具」里勾选 `web__web_search` / `web__read_webpage` / `web__get_weather` /
  `web__get_current_time` 才会看到这些工具。
- **插件与工具分离**：插件是安装、启用和配置的单位，工具是插件通过 `on_ai_tool` 显式开放的能力。
  提供 Agent 工具的插件同时显示在「插件管理」中，可在那里配置 Qwen / IndexTTS 等后端；
  「Agent 工具」页只控制模型调用权限，并列出每项能力的来源插件、默认授权和群级授权，不再重复提供插件配置入口；
  智能体和单群设置改为逐个勾选工具（如「语音朗读」「联网搜索」「天气查询」），
  策略字段由 `allowed_plugins` 改为 `allowed_tools`，启动时自动迁移旧配置。
- **TTS 服务空闲卸载**：Qwen3-TTS 和 IndexTTS 服务默认空闲 15 分钟后释放显存，下次请求自动重载
  （qwen 约 8 秒，IndexTTS 约 1 分钟）；`--idle-timeout` 可调整，`0` 保持常驻。缓存命中的请求
  不需要加载模型。`/health` 新增 `state`、`idle_seconds`、`idle_timeout`。
- **读取合并转发内容**：Agent 聊天设置的「附带信息」新增开关。开启后会把当前消息里的
  合并转发聊天记录展开成文本（发送者昵称、正文、图片占位，最多约 4000 字、2 层嵌套），
  随消息发给模型并写入历史；默认关闭，读取失败时仍只显示 `[合并转发]` 占位。
- **仅唤醒固定回复**：只发唤醒词时新增“发送固定文本（不调用模型）”模式。设置里每行
  填一条候选回复，随机发送一条；不消耗模型额度，仍照常写入会话历史。

### 修复

- **唤醒词单独发送不回应**：只发唤醒词（如“猫猫”）时会被当成空内容直接忽略；现在会照常
  交给模型自然回应，历史里仍记录用户原话。
- **网络抖动导致回复静默丢失**：模型请求遇到连接失败、超时等传输层错误时，会按 0.6 秒、
  1.5 秒自动重试两次；仍失败才记录“模型调用失败”。此前一次瞬时断网会让唤醒消息看起来像
  没被识别。

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
