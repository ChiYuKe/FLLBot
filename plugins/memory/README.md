# memory — 长期记忆

让智能体记住群友**本人明确要求保留**的事实与偏好，之后能在同一会话里回查、删除。记忆按「机器人账号 + 群/私聊 + 用户」隔离，只存在插件自己的 SQLite 文件里，不写入宿主数据库。

## 启用方式

1. 在「插件」页启用 `memory`（默认启用）。
2. 在「智能体 → 允许调用的工具」或单群设置里勾选需要的工具：
   - `memory__remember`（写）
   - `memory__recall`（读）
   - `memory__forget`（写）
3. 群聊需要该群启用群聊 AI 且授权对应工具；私聊需要私聊智能体开启（如果宿主版本支持）。

只启用插件、不授权工具时，命令 `/记忆`、`/忘记` 仍然可用；模型看不到记忆工具。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/记忆` | 查看你在当前群/私聊的长期记忆，最近 `list_limit` 条，带编号与日期 |
| `/忘记 <编号>` | 删除你本人的一条记忆 |
| `/忘记 全部` | 清空你在当前群/私聊的全部记忆 |

别名：`/记忆` 支持 `/memory`、`/回忆`；`/忘记` 支持 `/forget`、`/忘掉`。命令只操作本人记录，不会显示或删除别人的记忆。

## AI 工具

| 工具 | effect/risk | 能力 | 说明 |
| --- | --- | --- | --- |
| `memory__remember` | `side_effecting` / `write` | `memory.write` | 当前用户明确要求「记住……」时保存本人事实或偏好，返回记忆编号 |
| `memory__recall` | `read_only` / `read` | `memory.read` | 查当前用户在当前会话的记忆，可按关键词过滤，返回编号 |
| `memory__forget` | `side_effecting` / `write` | `memory.write` | 按编号删除，或 `all=true` 清空本人记忆 |

约束：

- 记忆主体固定为当前发言人，**工具参数里没有用户 id**，不能替别人记录或查询。
- 不能跨群、跨私聊、跨机器人账号，也不能在私聊里看到群记忆。
- 只在明确请求（@、唤醒词、私聊或显式命令）时写入；**自动参与时宿主沙盒会拒绝 `remember` / `forget`，返回 `{"error":"tool_blocked","reason":"automatic_side_effect","capability":"memory.write"}` 且不落库**。`recall` 是只读工具，自动参与时可用。
- `memory.write` 没有独立策略开关，和 `memory.read` 一样默认可用；不需要打开 `allow_write`，但仍受自动参与边界和本轮写入额度（`max_write_calls_per_turn`）约束。
- 密码、令牌、验证码、证件号等敏感内容会被拒绝；第三方隐私与猜测不应记录（见工具说明和提示词片段）。

## 配置

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | 开 | 是否启用长期记忆；支持单群覆盖 |
| `max_facts` | int | 50 | 同一账号、同一会话里每位用户的条数上限（1–200） |
| `max_content_chars` | int | 200 | 单条记忆长度上限（20–500），超出直接拒绝 |
| `retention_days` | int | 180 | 记忆保留天数（1–3650），过期自动清理 |
| `auto_hint` | bool | 开 | 是否向智能体注入记忆使用提示；关闭后工具仍可用 |
| `list_limit` | int | 10 | `/记忆` 默认显示条数（1–50） |

达到 `max_facts` 后新记忆会被拒绝，需要先 `/忘记` 旧记忆；重复内容不会重复占额度，只会刷新时间与过期时间。

## 数据与隐私

- 数据文件：`data/plugins/memory/memory.sqlite3`（WAL 模式），不触碰宿主数据库。
- 隔离键：`(机器人账号, scene, scene_id, 用户)`；`scene` 为 `group`（群号）或 `private`（用户号）。
- 清理：每次写入顺带清理过期记录，另有每小时周期清理；插件卸载不会删除数据。
- 备份实例时请同时备份 `data/plugins/` 与宿主数据库。

## 已知边界

- 写工具的 `effect` 使用宿主唯一合法的写语义 `side_effecting`（宿主 effect 枚举没有 `write`），`risk` 显式为 `write`；`memory.write` 能力默认放行，自动参与拒绝完全由宿主 `fllbot/sandbox/guard.py` 统一裁决，插件回调不再自行检查 `trigger_decision`。
- 该包要求宿主已提供 `memory.write` 能力（本仓库 0.2 之后版本）；旧宿主会把它当成未知能力并在插件加载时报错。
- 隐私判断是提示词级约束加少量确定性兜底，插件不能理解任意语境；对高风险内容应直接拒绝。

开发与维护说明见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。
