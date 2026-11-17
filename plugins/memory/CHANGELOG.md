# 变更记录

## 0.1.0

- 新增独立 `memory` 长期记忆插件：`memory__remember`、`memory__recall`、`memory__forget` 三个 AI 工具，分别声明 `side_effecting`/`write`、`read_only`/`read`。
- 记忆按「机器人账号 + 群/私聊 + 用户」隔离；工具参数不提供用户 id，跨会话不可见。
- 插件数据目录内自建 SQLite；支持每人条数上限、单条长度上限、保留天数与每小时过期清理。
- 新增 `/记忆`、`/忘记 <编号|全部>` 命令与中文别名，只操作本人记录。
- 新增记忆使用提示词片段：按工具可见性分别注入 remember/recall/forget 的使用说明与敏感信息禁令；可用 `auto_hint` 关闭。
- 自动参与时 `remember`/`forget` 返回 `addressed_required` 并拒绝落库，复用 poke/emotion 的 `trigger_decision.addressed` 先例。
