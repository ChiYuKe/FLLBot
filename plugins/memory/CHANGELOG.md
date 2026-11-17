# 变更记录

## 0.2.0

- 写工具 `memory__remember` / `memory__forget` 改为声明宿主能力 `memory.write`，自动参与拒绝从插件回调自检迁移到宿主沙盒统一裁决（`tool_blocked`、`reason=automatic_side_effect`、`capability=memory.write`）。
- 删除回调内的 `trigger_decision.addressed` 检查；明确请求仍由宿主 `write_requires_addressed` 放行，默认策略不需要打开 `allow_write`。
- `memory.write` 没有独立策略开关，与 `memory.read` 一样默认可用；插件要求宿主提供该能力（本仓库 0.2 之后版本）。

## 0.1.0

- 新增独立 `memory` 长期记忆插件：`memory__remember`、`memory__recall`、`memory__forget` 三个 AI 工具，分别声明 `side_effecting`/`write`、`read_only`/`read`。
- 记忆按「机器人账号 + 群/私聊 + 用户」隔离；工具参数不提供用户 id，跨会话不可见。
- 插件数据目录内自建 SQLite；支持每人条数上限、单条长度上限、保留天数与每小时过期清理。
- 新增 `/记忆`、`/忘记 <编号|全部>` 命令与中文别名，只操作本人记录。
- 新增记忆使用提示词片段：按工具可见性分别注入 remember/recall/forget 的使用说明与敏感信息禁令；可用 `auto_hint` 关闭。
- 自动参与时 `remember`/`forget` 返回 `addressed_required` 并拒绝落库，复用 poke/emotion 的 `trigger_decision.addressed` 先例。
