# 后台配置与数据作用域清单

基于当前源码，2026-11-02。表内仅展示代码默认值，不读取运行配置、环境密钥或真实数据。字段覆盖由 `scripts/check_admin_scope.py` 检查。

## 加载、保存和生效边界

运行时参数的加载顺序为代码默认 → config.toml → .env → 环境变量 → 数据库 meta.config_overrides；启动字段只从启动来源读取，不接受数据库运行时覆盖。数据库覆盖不会回写配置文件。单服务非空 api_key_env 环境值优先于配置密钥，缺失时回退 api_key。

“后续请求/消息”表示新处理使用新配置，已经开始的请求可能仍保留旧快照。“启动构造”表示必须修改启动配置来源并重建实例；运行中修改会返回 409，同一请求其他改动也不保存。旧库中的启动覆盖原子移入 meta.inactive_startup_overrides，保留值和字段审计，且不参与当前或下次启动的生效配置。后台只读展示启动配置值与历史覆盖。

全局配置保存在主库 meta；群配置保存于 groups，按 group_id 共享给所有 Bot。Bot 默认值保存于 kv 的 bot-profiles，以 self_id 为键。插件配置使用 plugin-settings:<插件>，0:<字段> 为全局，<群号>:<字段> 为群覆盖，不按 Bot 隔离。插件字段是否支持群覆盖以声明 schema.group_scoped 为准。

## 实例配置字段

| 字段 | 代码默认值 | 唯一归属 | 生效边界 |
| --- | --- | --- | --- |
| `server.host` | `"127.0.0.1"` | 实例连接 | 启动构造；修改启动配置来源并重建实例 |
| `server.port` | `6701` | 实例连接 | 启动构造；修改启动配置来源并重建实例 |
| `server.ws_path` | `"/onebot/v11/ws"` | 实例连接 | 连接地址展示；路由另有固定别名与通配兼容 |
| `server.access_token` | `""` | 实例连接 | 新连接鉴权；既有连接不主动断开 |
| `server.heartbeat_timeout` | `120.0` | 实例连接 | 后续请求/消息；保存后不追溯已开始操作 |
| `server.call_timeout` | `30.0` | 实例连接 | 新连接创建时读取；既有连接保持旧超时 |
| `server.cors_origins` | `["*"]` | 实例连接 | 启动构造；修改启动配置来源并重建实例 |
| `admin.enabled` | `true` | 后台访问 | 启动来源；关闭后台页面、管理 API、文档和事件流，保留健康检查、版本与 OneBot |
| `admin.token` | `""` | 后台访问 | 设置非空值时动态切换；空值不能直接撤销当前内存令牌 |
| `admin.token_file` | `"data/admin_token.txt"` | 后台访问 | 启动构造；修改启动配置来源并重建实例 |
| `bot.superusers` | `[]` | 命令与身份 | 后续请求/消息；保存后不追溯已开始操作 |
| `bot.command_prefix` | `["/"]` | 命令与身份 | 后续请求/消息；保存后不追溯已开始操作 |
| `bot.allow_bare_command` | `true` | 命令与身份 | 后续请求/消息；保存后不追溯已开始操作 |
| `bot.error_reply` | `""` | 命令与身份 | 后续请求/消息；保存后不追溯已开始操作 |
| `permission.allow_groups` | `[]` | 访问与权限 | 后续请求/消息；保存后不追溯已开始操作 |
| `permission.group_access` | `"auto"` | 访问与权限 | 后续请求/消息；保存后不追溯已开始操作 |
| `permission.deny_groups` | `[]` | 访问与权限 | 后续请求/消息；保存后不追溯已开始操作 |
| `permission.default_group_enabled` | `true` | 访问与权限 | 未显式覆盖或已恢复继承的群跟随变化；既有显式值保留 |
| `permission.group_admin_permission` | `true` | 访问与权限 | 后续请求/消息；保存后不追溯已开始操作 |
| `permission.allow_private` | `true` | 访问与权限 | 后续请求/消息；保存后不追溯已开始操作 |
| `plugin.dirs` | `["plugins"]` | 插件运行 | 启动构造；修改启动配置来源并重建实例 |
| `plugin.builtin` | `true` | 插件运行 | 启动构造；修改启动配置来源并重建实例 |
| `plugin.hot_reload` | `true` | 插件运行 | 启动构造；修改启动配置来源并重建实例 |
| `plugin.watch_interval` | `2.0` | 插件运行 | 启动构造；修改启动配置来源并重建实例 |
| `plugin.max_concurrency` | `8` | 插件运行 | 启动构造；修改启动配置来源并重建实例 |
| `plugin.max_pending_events` | `1000` | 插件运行 | 启动构造；修改启动配置来源并重建实例 |
| `plugin.handler_timeout` | `60.0` | 插件运行 | 后续请求/消息；保存后不追溯已开始操作 |
| `storage.path` | `"data/fllbot.db"` | 存储维护 | 启动来源确定实际数据库；修改值不迁移数据 |
| `storage.backup_dir` | `"data/backups"` | 存储维护 | 后续备份操作读取；旧目录备份需另外管理 |
| `logging.level` | `"INFO"` | 运行日志 | 后续请求/消息；保存后不追溯已开始操作 |
| `logging.dir` | `"logs"` | 运行日志 | 启动构造；修改启动配置来源并重建实例 |
| `logging.json_lines` | `true` | 运行日志 | 启动构造；修改启动配置来源并重建实例 |
| `logging.file` | `"fllbot.log"` | 运行日志 | 启动构造；修改启动配置来源并重建实例 |
| `logging.ring_size` | `500` | 运行日志 | 启动构造；修改启动配置来源并重建实例 |
| `llm.context_window` | `null` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.supports_tools` | `true` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.supports_images` | `true` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.supports_temperature` | `true` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.token_parameter` | `"max_tokens"` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.image_token_budget` | `4096` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.enabled` | `false` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.base_url` | `"https://api.deepseek.com"` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.api_key` | `""` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.model` | `"deepseek-chat"` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.timeout` | `60.0` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.max_tokens` | `800` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.temperature` | `0.8` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.context_mode` | `"model"` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.max_context_messages` | `16` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.max_context_chars` | `4000` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.enable_private` | `true` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.enable_group` | `true` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.trigger` | `"at"` | 模型默认 | 兼容展示；Agent 触发使用插件设置和群策略 |
| `llm.trigger_prefix` | `"ai"` | 模型默认 | 兼容字段；Agent 唤醒实际使用 chat.wake_word/Bot 设置/昵称 |
| `llm.persona` | `""` | 人格默认 | 后续请求；群人格 → Bot 人格 → 系统人格 → 兜底 |
| `llm.chunk_size` | `500` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.cooldown_seconds` | `3.0` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.system_extra` | `""` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.default` | `"default"` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.providers` | `{}` | 模型默认 | 后续请求；服务字典内各字段见下表 |
| `llm.routes` | `{}` | 模型默认 | 后续请求；群/Bot 指定优先于任务/场景路由 |
| `llm.fallback_providers` | `[]` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.group_daily_token_limit` | `0` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.group_daily_cost_limit` | `0.0` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.max_concurrency` | `2` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.input_price` | `0.0` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `llm.output_price` | `0.0` | 模型默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.send_nickname` | `true` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.send_user_id` | `true` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.send_role` | `true` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.send_time` | `false` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.allow_images` | `false` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.read_forward` | `false` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.group_memory` | `true` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.memory_session_gap_minutes` | `30` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.memory_recent_messages` | `200` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.memory_context_chars` | `24000` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.reply_mode` | `"auto"` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.forward_threshold` | `1200` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.reply_chunk_size` | `0` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.reply_interval_ms` | `350` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.quote_reply` | `false` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.at_sender` | `false` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.wake_word` | `""` | 聊天默认 | 后续消息；全局非空时优先于 Bot 唤醒词和昵称 |
| `chat.wake_only_mode` | `"prompt"` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.wake_only_prompt` | `"（用户只叫了你的名字，没有说具体内容，请简短自然地回应。）"` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `chat.wake_only_replies` | `[]` | 聊天默认 | 后续请求/消息；保存后不追溯已开始操作 |
| `retention.enabled` | `false` | 数据维护 | 后续定时检查；保存不立即清理 |
| `retention.interval_seconds` | `3600` | 数据维护 | 后续定时检查；调度器每 60 秒检查一次 |
| `retention.messages_days` | `0` | 数据维护 | 后续请求/消息；保存后不追溯已开始操作 |
| `retention.conversations_days` | `0` | 数据维护 | 后续请求/消息；保存后不追溯已开始操作 |
| `retention.usage_days` | `0` | 数据维护 | 后续请求/消息；保存后不追溯已开始操作 |
| `retention.changes_days` | `0` | 数据维护 | 后续请求/消息；保存后不追溯已开始操作 |

## 单个模型服务字段

`*` 为服务标识，实际保存于 llm.providers。服务支持能力、价格和并发字段仅在显式设置时覆盖全局；base_url/api_key/model 为空及 timeout/max_tokens/temperature 为 null 时继承全局。no_stream 和 api_key_env 使用服务自身值。字段为共享资源，群/Bot 只保存服务标识，不复制凭据。

| 字段 | 代码默认值 | 唯一归属 | 生效边界 |
| --- | --- | --- | --- |
| `llm.providers.*.context_window` | `null` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.supports_tools` | `true` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.supports_images` | `true` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.supports_temperature` | `true` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.token_parameter` | `"max_tokens"` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.image_token_budget` | `4096` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.base_url` | `""` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.api_key` | `""` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.api_key_env` | `""` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.model` | `""` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.timeout` | `null` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.max_tokens` | `null` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.temperature` | `null` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.no_stream` | `false` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.input_price` | `0.0` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.output_price` | `0.0` | 模型服务 | 后续请求；继承规则见上文 |
| `llm.providers.*.max_concurrency` | `2` | 模型服务 | 后续请求；继承规则见上文 |

## Bot 默认值

保存于 bot-profiles，影响该 Bot 未被群覆盖的行为。昵称来自连接或已同步的资料；昵称同步不应替换默认行为。后台 Agent → 账号默认设置支持在线/离线账号编辑；版本仅包含该账号默认值，昵称同步不造成编辑冲突。保存默认值和字段级审计同一事务提交。

| 字段 | 代码默认值 | 唯一归属 | 生效边界 |
| --- | --- | --- | --- |
| `bot_defaults.reply_mode` | `"inherit"` | 机器人默认值 | 后续消息；reply_mode=inherit 或空资源标识跟随上层 |
| `bot_defaults.wake_word` | `""` | 机器人默认值 | 后续消息；reply_mode=inherit 或空资源标识跟随上层 |
| `bot_defaults.provider` | `""` | 机器人默认值 | 后续消息；reply_mode=inherit 或空资源标识跟随上层 |
| `bot_defaults.persona` | `""` | 机器人默认值 | 后续消息；reply_mode=inherit 或空资源标识跟随上层 |

## 群策略

保存于 groups.config.policy；下表 null 表示不提供该项覆盖，空模型标识跟随上层。触发方式支持 trigger_modes 组合，旧 trigger_mode 单选仍兼容。群的 enabled、准入名单、groups.config.llm.enabled/persona 是独立维度，不与策略字段混为同一开关。移除覆盖保留历史及其他插件数据。

| 字段 | 代码默认值 | 唯一归属 | 生效边界 |
| --- | --- | --- | --- |
| `group_policy.name` | `""` | 群单独配置 | 后续消息；同群多个 Bot 共享覆盖 |
| `group_policy.auto_group` | `null` | 群单独配置 | 后续消息；同群多个 Bot 共享覆盖 |
| `group_policy.trigger_mode` | `null` | 群单独配置 | 后续消息；同群多个 Bot 共享覆盖 |
| `group_policy.trigger_modes` | `null` | 群单独配置 | 后续消息；同群多个 Bot 共享覆盖 |
| `group_policy.trigger_prefix` | `null` | 群单独配置 | 保留兼容字段；当前实际唤醒词见 chat.wake_word 与 Bot 默认值 |
| `group_policy.cooldown_seconds` | `null` | 群单独配置 | 后续消息；同群多个 Bot 共享覆盖 |
| `group_policy.auto_reply_probability` | `null` | 群单独配置 | 后续消息；同群多个 Bot 共享覆盖 |
| `group_policy.max_tool_rounds` | `null` | 群单独配置 | 后续消息；同群多个 Bot 共享覆盖 |
| `group_policy.allowed_tools` | `null` | 群单独配置 | 后续消息；同群多个 Bot 共享覆盖 |
| `group_policy.provider` | `""` | 群单独配置 | 后续消息；同群多个 Bot 共享覆盖 |
| `group_policy.system_extra` | `""` | 群单独配置 | 后续消息；同群多个 Bot 共享覆盖 |

## 继承、权限和旧数据

- 模型：群 policy.provider → Bot defaults.provider → 任务/场景路由 → 系统默认 → 配置兼容回退。人格：群 llm.persona → Bot defaults.persona → llm.persona → 兜底。
- 响应：插件默认设置 → Bot reply_mode → 插件旧群覆盖 → groups.config.policy；触发方式组合以群策略为准。唤醒词由全局 chat.wake_word 非空值、Bot wake_word、昵称依次确定。
- 群 deny_groups 先于允许名单与群 enabled；group_access=auto 兼容旧白名单，listed 可以空名单拒绝全部。私聊由 allow_private 单独约束。插件全局明确停用优先于所有群覆盖；声明默认关闭且未全局明确停用时，群仍可单独开启。群覆盖保留，重新全局启用后继续生效。
- 旧 allowed_plugins 会通过迁移展开为具体 allowed_tools。旧插件群覆盖仍可读；群策略和插件设置不同存储层同时存在时，应使用生效来源检查。人格不再内置于包中，用户文件在项目 personas/；目录顺序后者覆盖前者。
- 插件字段按 GET /api/plugins/<名称>/settings 的 schema 查看默认值、secret、group_scoped 和类型。全局/群原值由插件管理，群响应旧层不会由读取接口静默迁移。secret 默认值和生效值经管理 API 遮盖。

## 数据隔离与维护

| 数据 | 隔离键/存储 | 保留和维护边界 |
| --- | --- | --- |
| 全局覆盖 | 主库 meta.key | 全实例；不被业务数据清理删除 |
| Bot 资料 | kv(bot-profiles,self_id) | 按账号；不被业务清理删除 |
| 群配置 | groups.group_id | 同群所有 Bot 共享；移除覆盖不清历史 |
| 插件开关 | plugin_state(plugin_name,group_id) | 0 全局，群级不区分 Bot |
| 插件配置 | kv(plugin-settings:<名称>,<群号>:<字段>) | 0 全局；群覆盖不区分 Bot；批量原子保存并记录字段级元数据 |
| AI 旧会话 | chat_messages(scene,scene_id) | 无 self_id；同群多 Bot 共享；会话清理联动记忆和摘要 |
| 群记忆原文/摘要 | group_memory_*(self_id,group_id) | 按 Bot 和群隔离；部分原文删除使相关摘要失效 |
| 消息记录 | message_log(self_id,scene,scene_id) | 按 Bot 可筛选；手动预览与可选保留期 |
| 模型用量 | llm_usage(scene,scene_id,provider,model) | 无 self_id；费用为估算；清理会减少统计，最近一天不清理 |
| 消息诊断 | message_traces(self_id,scene,scene_id,message_id) | 元数据；7 天/10000 条上限；手动可提前清理 |
| 管理变更 | management_changes(group_id,id) | 0 为全局；字段元数据不含凭据/正文；单令牌不能识别具体个人 |
| 清理结果 | cleanup_runs.id | 范围和数量；最多 1000 条；不含原文 |
| 旧主库提醒 | reminders(scene,scene_id) | 历史兼容表，无 self_id；不作为 daily 任务来源 |
| daily 任务 | 插件 daily.sqlite3 的 tasks(bot,scene,owner) | 正 scene 为群，负为私聊；无时区时间按 UTC+08:00；主库备份不包含 |
| daily 运行记录 | 插件 task_runs(task_id,id) | 最多 10000 条元数据；发送中断不自动重发 |
| daily 群资料 | 插件 knowledge(bot,scene,title) | Bot/会话隔离，管理员维护；不在主库清理范围 |
| 运行日志 | logging.dir/file 与内存 ring | 实例级；沿用日志轮转，不在业务数据清理中 |
| 数据库备份 | storage.backup_dir | 含主库配置/历史，可能含密钥；不含人格/插件文件数据；停机整库恢复 |

## 验收发现及下一步

已完成的证据见各功能测试与 docs/admin-roadmap.md 的实施记录；此清单本身不等于整体验收通过。当前还需核对并修正：其余管理对象的审计覆盖，并完成全局插件停用页面验证。该页面验证目前受浏览器自动审批用量限制，尚未执行。P4 多人权限和远程部署继续按需暂缓。
