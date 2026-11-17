# Agent 与插件：工具目录、执行解析与结果复用

这份文档说明智能体（`agent` 插件）与业务插件之间的工具契约。相关实现：

```text
fllbot/plugin/base.py     插件声明 on_ai_tool，注册到 plugin.ai_tools
fllbot/tool_registry.py   宿主工具目录：快照、签名、定义模板、按需重建
fllbot/builtin_plugins/agent/plugin.py
                          本群可见性过滤、提示词说明、单工具解析
fllbot/tool_executor.py   权限/沙盒/钩子/执行事实，以及同轮读取结果复用
fllbot/llm/agent_runtime.py
                          模型循环、工具调用协议与重复提醒
```

## 1. 职责边界

| 角色 | 负责 | 不负责 |
| --- | --- | --- |
| 业务插件 | 用 `on_ai_tool` 声明工具、参数 Schema、能力、风险、超时和执行逻辑 | 判断某群是否授权、是否可执行 |
| `ToolRegistry` | 汇总所有插件工具、缓存 JSON 定义模板、按变化重建 | 群策略、用户权限、模型调用 |
| `agent` 插件 | 按群设置、插件开关、工具授权、记忆条件筛出“本轮可见工具”，给模型提供说明 | 复制执行逻辑、直接写存储 |
| `ToolExecutor` | 参数校验、实时权限复核、沙盒裁决、钩子、执行事实、结果缓存 | 决定模型看到哪些工具 |
| 管理 API / WebUI | 展示工具目录、风险、能力与授权状态 | 直接执行工具 |

插件只声明一次；宿主负责“给模型看什么”和“实际能不能做”。

## 2. 工具目录快照

`ToolRegistry` 从当前已加载插件的 `ai_tools` 构造不可变快照：

- 每个工具包含 `id`（`插件名__工具名`）、标题、说明、权限、`effect`、`risk`、`capabilities`、
  超时、输出上限和预构造的模型定义模板。
- 快照签名包含插件对象标识、工具对象标识、权限、effect、risk、能力、超时、输出上限以及
  Schema 的摘要；插件加载、卸载、热重载或原地修改 Schema 后会重建。
- 没有变化时 `snapshot()` 直接复用上一次结果，并累计 `reused`；重建累计 `rebuilds`，
  `revision` 递增。`GET /api/status` 与 `GET /api/tools` 的 `registry` 可查看这些计数。
- 模型定义模板只读；交给模型时用 `definition_copy()` 深拷贝，校验失败的请求不会污染
  下一次请求。

## 3. 本群可见工具

`agent` 插件在每轮开始时调用一次目录快照，再按以下顺序过滤：

1. 插件仍加载、插件开关允许；
2. 工具 ID 在群策略 `allowed_tools`（或账号默认工具授权）中；
3. 工具自身 `permission` 对当前发送者通过；
4. Agent 自有工具的特殊条件：`search_history` 仅在群记忆开启且本轮确实提到过去时出现；
5. 自动参与且模型支持工具时，额外提供内部 `agent__stay_silent`。

只有入选工具才深拷贝 Schema、按名称排序后发给模型。工具列表和说明在模型请求前缀中保持稳定，
便于服务商提示词缓存命中。

## 4. 执行时的单工具解析

执行阶段不再重建整份目录。`_run_tool` 捕获本轮快照，`ToolExecutor` 通过 `resolve(name)` 只解析目标工具：

- 确认工具仍在登记表中、所属插件对象未被替换、插件仍启用；
- 重新读取**实时**群策略：`allowed_tools` 被移除时立即拒绝；
- 重新检查工具权限与 Agent 记忆条件；
- 与发起执行时的 spec 比较 callback、Schema、effect、能力与风险，任一变化都拒绝旧调用。

这样既保留了“等待排队期间撤销授权也生效”的安全性，又避免了每次权限复核都遍历全部插件和 Schema。
被拒绝的调用不执行回调、不写 `tool_executions`、不产生副作用。

参数校验失败后，宿主会按 Schema 做一次**有限、无损的类型自愈**再严格校验：

- 纯数字字符串 → 整数/数字；`"true"`/`"false"`（不区分大小写）→ 布尔；数字/布尔 → 文本；
  单值 → 数组（包成 `[value]`）；以 `{` 开头且可解析的 JSON 字符串 → 对象；
  数组 `items` 与对象 `properties` 递归处理，浮点整数值可无损转为整数；
- 不做未知字段、缺失必填、`enum`/`const`/`format`/`min`/`max`/长度等约束违规的“修复”，
  也转换不了无法无损转换的值；修正后仍用 `Draft202012Validator` 严格校验，失败照旧返回 `invalid_arguments`；
- 修正成功只记录一条 `tool / arguments_repaired` 追踪（只含工具名），不写原始或修正后的参数值；
  后续的沙盒裁决、缓存键、重复计数与执行记录都使用自愈后的参数。

## 5. 同轮读取结果复用

`ToolExecutor` 对**同一轮、同一账号、同一工具、相同规范化参数**的重复调用做短期结果复用：

- 仅限 `effect = read_only` 且能力全部属于 `fs.read`、`net.read`、`memory.read`、`system.read` 的工具；
- 带任何写入、发送、管理类能力的工具一律不缓存；
- 工具有 `on_tool_hook` 时不缓存，保持钩子对每次调用的裁决能力；
- 只在 `before` 钩子、实时权限与沙盒裁决通过后才命中；命中只写一条 `cache_hit` 追踪，不写存储；
- 缓存按键包含轮次 ID，跨消息不会复用；最多 128 条、300 秒，关闭/重开执行器时清空；
- `ToolExecutor.status()` 暴露 `cache_entries` 与 `cache_hits`。

它解决的是模型在同一轮里反复查询同一页面、同一文件或同一状态的问题；不会让“重新读取以获取最新数据”
的显式要求失效——换参数或换一轮都会重新执行。

同轮相同工具 + 相同规范化参数（含自愈后的参数）的 `read_only` 调用还有**硬上限**：

- 超过 `ToolExecutor.repeat_limit`（默认 12）次后不再执行，返回 `error = tool_repeat_limit`
  和中文 `message`，提示复用已有结果或调整参数；
- 只约束 `read_only`；写入、发送、管理类（`idempotent`/`side_effecting`）不设此限，仍沿用既有的
  3/5/8 次重复提醒；
- 计数键为“轮次 + 账号 + 工具 + 参数哈希”，最多保留 256 个，超出淘汰最旧；不写数据库、不跨轮复用；
- `ToolExecutor.status()` 暴露 `repeat_keys` 与 `repeat_blocked`，计数器在 `reopen()` 时清空。

同一轮模型请求多个工具时，如果**整批都是已注册的只读工具**，`AgentRuntime` 会并发执行（最多 4 个），
结果仍按模型给出的调用顺序写回；只要有一个写入、发送、管理工具，整批恢复串行。这样多网页、多文件
读取不会互相等待，而副作用顺序保持不变。

### 5.1 直出回复约定（silent + reply）

工具若已经完成用户可见交付（发文件、发图、改配置等），成功时返回
`{"silent": true, "reply": "<一句自然中文确认，≤60 字，不含工具名/JSON/内部 id>", ...}`：

- `AgentRuntime` 收集整批结果；只有**整批全部 silent、无失败（含 `sent = false`）、无待处理追问**
  时才提前结束并向调用方返回 `RuntimeResult(reason='tool_completed', reply=合并后的文本)`；
- 多个 `reply` 用换行合并并去重，单条限长，避免重复描述；
- 混合批次里只要还有非 silent 的数据结果，宿主就继续下一轮模型，绝不因某个工具 silent 丢掉其他结果；
- 失败/未发送批次按失败方向处理（追加失败提醒并让模型继续），不会提前收尾；
- `spoken` 保留给“已经通过音频表达、无需文字”的工具（如 `voice__speak`），只有 `spoken`、没有
  `reply` 时不得发送文字；
- `agent_conversation` 在 `result.text` 为空而 `result.reply` 非空时，复用现有发送回调（引用、
  长回复、发送事实与历史投影）发送这句确认。

## 6. 失败信息契约

所有失败结果在既有结构化字段（`error`，沙盒拒绝时的 `reason`/`hint`/`capability`/`risk`）之外
增加 `message`：一句中文、模型可直接转述，且不包含参数值、本机路径或用户正文。覆盖：

- 环境/权限：`service_stopping`、`tool_executor_busy`、`tool_unavailable`、`tool_permission_denied`、
  `context_cleared`、`tool_hook_failed`、`tool_rejected`、`call_identity_conflict`；
- 执行事实：`tool_failed`、`tool_uncertain`（明确“可能已执行，不要自动重试”）、`tool_cancelled`、
  `tool_succeeded`、`tool_running`；
- 策略与上限：沙盒 `tool_blocked`（沿用沙盒自身 hint，不扩大其隐私边界）、`tool_repeat_limit`；
- 参数与结果整理：`invalid_arguments`、`tool_result_processing_failed`。

`invalid_arguments` 的 `details` 同时给出人类可读信息：

- `path`：JSON Schema 绝对路径列表（保留旧字段）；`field`：如 `rows[1][1]` / `content`；
- `constraint`：失败的约束关键字（保留旧字段）；`expected`：只来自 Schema 的约束值（保留旧字段）；
- `expected_summary`：中文期望摘要；`hint`：修正建议；`description`：Schema 中该字段的说明（若有）；
- JSON 解析失败保留 `line`/`column` 与 `hint`。所有这些字段都不回传模型给的原始参数值。

## 7. 性能与成本

| 优化 | 之前 | 现在 |
| --- | --- | --- |
| 工具定义构造 | 每轮、每次权限复核都遍历插件并复制 Schema | 快照模板复用，只为入选工具深拷贝一次 |
| 插件扫描 | 每次 `refresh` 全量 `plugins.loaded()` | 每轮一次快照；执行期 O(1) 解析 + 实时权限检查 |
| 重复读取 | 每次模型重复调用都执行并写执行事实 | 同轮相同参数直接复用结果 |
| 参数类型偏差 | 直接返回 `invalid_arguments` | 无损自愈后再严格校验，失败附带中文修正提示 |
| 重复只读循环 | 无硬上限，仅 3/5/8 次提醒 | 同轮同参超过 12 次拒绝执行 |
| 批量只读 | 逐个等待 | 整批只读最多 4 路并发，结果顺序不变 |
| 模型提示词 | 工具按名称排序，Schema 稳定 | 目录签名稳定，工具列表前缀更稳定 |

工具目录与结果缓存都不改变权限、沙盒和送达语义：它们只减少重复工作，不扩大能力。

## 8. 新增工具时的约定

```python
@plugin.on_ai_tool(
    "lookup",
    effect="read_only",
    capabilities=("net.read",),
    title="查询",
    description="...",
    parameters={...},
)
async def lookup(ctx: Context, ...) -> dict: ...
```

- 声明真实 `capabilities` 与 `effect`：读取类工具才可能享受同轮结果复用；
- 不要在同一工具里混合读写；拆成两个工具，宿主才能分别裁决和缓存；
- 工具结果只返回模型需要的内容，不返回本机路径、令牌或原始异常；
- 需要每次强制刷新的操作，应声明写能力或提供参数区分，而不要依赖缓存关闭；
- 完成用户可见交付时返回 `silent=true` 与 ≤60 字 `reply`；已通过音频表达则用 `spoken` 而不是文字；
- 失败时返回结构化 `error`，让宿主的失败契约与提醒接管，不要假装成功或自行重试外部动作。

## 9. 能力范围

哪些请求该由哪个工具承接、哪些能力默认关闭、哪些刻意不提供（本机命令、PDF/Office、ZIP 解压等），
统一记录在 [群聊 Agent 能力清单](agent-capabilities.md)，新增插件前先核对，避免越界或重复实现。

## 10. 验证

```powershell
.venv/Scripts/python.exe -m pytest tests/test_tool_registry.py tests/test_tool_cache.py \
  tests/test_agent_extensions.py tests/test_execution_config.py tests/test_tool_executor.py \
  tests/test_tool_resilience.py tests/test_agent_direct_reply.py -q
.venv/Scripts/python.exe scripts/check_architecture.py
```
