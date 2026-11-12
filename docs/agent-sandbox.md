# 群聊智能体：参与判断、操作裁决与执行沙盒

这份文档说明 FLLBot 如何让智能体在群里“像个正常群友一样”参与，同时把模型发起的操作
放进可解释、可收紧的执行边界。相关实现：

```text
fllbot/participation.py   自动参与判断（确定性、可解释、不请求模型）
fllbot/trigger_policy.py  触发决策 + 参与度状态（最近回复、重复、@/唤醒词）
fllbot/sandbox/           能力声明、策略解析、操作裁决、工作区文件、受控联网
fllbot/tool_executor.py   执行前裁决、拒绝回执、本轮额度与审计
fllbot/builtin_plugins/workspace/  使用沙盒的示例工具包
```

## 1. 为什么需要三层

| 层 | 回答的问题 | 失败时的后果 |
| --- | --- | --- |
| 参与判断 | 这条普通群消息值得请求模型吗？ | 每条闲聊都调用模型，费用与延迟高、容易抢话 |
| 操作裁决 | 模型请求的这个工具，在当前群、当前发起者、当前触发方式下允许吗？ | 自动参与时产生写入、联网或群管理副作用 |
| 执行沙盒 | 即使允许，能碰哪些文件、主机和多少数据？ | 越权读取工作区外文件、访问内网、超大响应拖垮进程 |

三层都是**确定性代码**，不把安全边界交给模型自觉。模型只负责理解请求和选择工具；
是否执行、能执行到什么范围由宿主决定，拒绝结果会作为工具结果返回给模型，让它如实说明限制。

## 2. 自动参与判断

### 信号

`participation.evaluate()` 只读取当前消息与少量短期状态，不读历史正文：

| 因子 | 分值 | 说明 |
| --- | --- | --- |
| `directed_elsewhere` | -100 | 开头 @ 的是别人 |
| `addressed` | +100 | 被 @ |
| `wake_word` | +80 | 以唤醒词开头 |
| `reply_to_bot` | +30 | 引用了消息，且 Bot 在 5 分钟内说过话 |
| `mentions_bot` | +20 | 正文提到 Bot 名字但未直接呼叫 |
| `question` | +15 | 含“吗/呢/怎么/为什么/？”等提问标记 |
| `media` | +10 | 带图片、语音、文件或合并转发 |
| `thread_continuation` | +20 | Bot 最近 2 分钟内说过话 |
| `repeated` | -20 | 与最近 6 条群消息重复 |
| `laughter_only` | -30 | 纯“哈哈哈/草/xswl/2333/标点” |
| `acknowledge_only` | -20 | 纯“嗯/哦/好/收到/6” |
| `too_short` | -15 | 去空白后不超过 1 个字符 |
| `bot_dominant` | -15 | Bot 2 分钟内已回复 4 次以上且本条未 @ |

分数达到阈值才继续走模型请求；`agent` 插件的 `participation_threshold`（默认 `-10`）按群可覆盖。
`-100` 表示关闭筛选，恢复“所有自动参与消息都交给模型判断”。

### 设计取舍

- 只做**减法**：默认阈值很低，只过滤明显不需要接话的消息；真正是否开口仍由模型的
  `agent__stay_silent` / `<NO_REPLY>` 决定。
- 被 @、唤醒词、提问、接话始终高分，不会因为“太安静”漏掉明确请求。
- 判断过程不落正文，只把 `participation_below_threshold` 记为触发决策原因，便于在
  “消息追踪 → 触发决策”里解释为什么没调用模型。
- 计数器按“Bot 账号 + 群”隔离，内存有界；重启后重新积累，不写入数据库。

## 3. 工具能力声明

插件在 `on_ai_tool(...)` 中声明能力与风险：

```python
@plugin.on_ai_tool(
    "write_file",
    effect="side_effecting",
    capabilities=("fs.write",),
    risk="write",              # 可省略，但不能低于能力推导出的下限
    title="写入沙盒文件",
    description="...",
    parameters={...},
)
```

| 能力 | 含义 | 推导风险 |
| --- | --- | --- |
| `fs.read` / `fs.write` | 读/写工作区文件 | read / write |
| `net.read` / `net.write` | 读取/提交网络数据 | read / write |
| `qq.send` | 发送消息或媒体 | write |
| `qq.manage` | 群成员、群设置等管理动作 | destructive |
| `memory.read` | 读取本群记忆 | read |
| `system.read` | 读取机器人运行状态 | read |
| `process.exec` | 执行本机程序（当前未提供执行器） | destructive |

未声明能力的旧工具保持原有“插件权限 + 处理器权限”行为；声明后才会进入沙盒裁决。
`risk` 不能低于能力下限，避免插件把 `fs.write` 标成只读来绕过策略。

## 4. 策略解析

全局配置在 `[sandbox]`，单群可以在群策略中覆盖：

| 单群字段 | 含义 |
| --- | --- |
| `sandbox_write` | 是否允许本群工具写工作区 |
| `sandbox_network` | 是否允许本群联网 |
| `sandbox_private_network` | 是否允许访问本机/局域网 |

每次工具调用前重新解析，配置或群策略修改立即生效；单群覆盖只影响对应群。
`workspace` 相对项目根目录解析，默认 `data/agent_workspace`（随 `data/` 忽略，不提交）。

主要开关：`allow_read`、`allow_write`、`allow_network`、`allow_network_write`、
`allow_private_network`、`allow_qq_send`、`allow_destructive`、`allow_process`、
`destructive_requires_superuser`、`write_requires_addressed`。

## 5. 裁决顺序

1. 沙盒关闭 → 放行（由原有权限系统负责）。
2. 工具未声明能力 → 放行，标记 `legacy_tool`。
3. 逐个能力检查开关；任一关闭即拒绝。
4. 参数中的路径：拒绝绝对路径、盘符、`~`、`..`、NUL、敏感文件名。
5. 参数中的链接：只允许 http(s)，拒绝凭据、非白名单主机、内网/回环/链路本地/保留地址。
6. 写入/管理类：自动参与且未明确请求 → 拒绝；破坏性操作要求超级管理员；群管理要求群主、
   管理员或超级管理员。
7. 本轮额度：总调用数、写入次数；只有真正执行才计数。
8. 钩子（`on_tool_hook`）与工具回调。

拒绝返回 `{"error": "tool_blocked", "reason": ...}`，附带能力、风险和中文提示，
不回显参数值、本机绝对路径或 DNS 结果。常见原因：

| reason | 含义 |
| --- | --- |
| `write_disabled` / `read_disabled` | 对应能力未开放 |
| `network_disabled` / `network_write_disabled` | 联网或提交未开放 |
| `network_host_not_allowed` / `network_host_denied` | 主机不在白名单/被黑名单命中 |
| `network_private_denied` | 目标是本机、局域网或保留地址 |
| `path_absolute` / `path_escape` / `path_denied` | 绝对路径、越界或敏感文件 |
| `automatic_side_effect` | 自动参与时尝试写入/管理 |
| `authority_required` | 需要群管理或超级管理员 |
| `turn_quota_exceeded` / `turn_write_quota_exceeded` | 本轮调用或写入次数达到上限 |
| `process_disabled` | 未开放本机程序执行 |

## 6. 工作区文件沙盒

`ctx.sandbox.files` 提供 `read_text`、`write_text`、`list_dir`、`exists`：

- 路径必须是工作区相对路径；解析后再次做 `relative_to(root)` 检查，软链接逃逸会失败。
- 每次读写都有字节上限；超限读取返回 `truncated`，超限写入直接拒绝。
- 敏感路径默认拒绝：`.env*`、`config.toml`、`data/admin_token.txt`、数据库、备份、日志、
  `.git`、`*.pem/*.key/*.pfx`，可按 `deny_paths` 调整。
- 目录不存在时写入会自动创建（仍在工作区内）；读取不存在的文件返回 `not_found`。

工具里推荐直接使用 `ctx.sandbox.files`，不要自行 `open()`；自行打开会绕过围栏，
属于插件作者的责任，宿主无法拦截任意 Python 代码。

## 7. 受控联网

`ctx.sandbox.network.fetch(url)`：

- 只允许 `http` / `https`，拒绝 URL 内嵌用户名密码。
- 先做主机白/黑名单，再解析 DNS；解析到回环、私网、链路本地、保留地址时拒绝。
- 每次重定向都重新校验目标主机与地址，次数受 `max_redirects` 限制。
- 只返回文本类响应（`text/*`、JSON、XML），按 `max_fetch_bytes` 截断。
- 代理环境变量由 httpx 默认读取；需要强制直连时在部署层关闭代理。

DNS 检查降低但不能完全消除 DNS 重绑定风险；对不可信目标应同时使用主机白名单。

## 8. 观测与审计

- `ctx.trace_event('tool', 'tool_blocked', ...)` 写入消息追踪，只含工具名与原因码。
- 沙盒服务保留最近 100 条裁决元数据（时间、工具名、群号、是否允许、原因、能力、风险），
  不保存参数与正文；`GET /api/sandbox` 返回策略摘要、计数与最近 20 条。
- `GET /api/tools` 的每个工具新增 `risk` 与 `capabilities`，管理端可据此说明为什么某工具
  在某些群不可用。
- `/api/sandbox?group_id=<群号>` 可预览某个群实际生效的能力开关。

## 9. 示例：只允许一个群写工作区

```toml
[sandbox]
allow_read = true
allow_write = false
allow_network = true
allowed_hosts = ["docs.python.org", "*.github.com"]
```

然后只给该群加覆盖：

```json
{"enabled": true, "policy": {"sandbox_write": true}}
```

再在「智能体 → 允许调用的工具」中勾选 `workspace__read_file`、`workspace__write_file`；
自动参与仍然不会写入，群友 @ 或使用唤醒词明确要求后才会执行。

## 10. 边界

- 这是**进程内策略与资源围栏**，不是操作系统级沙箱。插件仍以宿主进程权限运行；
  只安装可信插件，`process.exec` 默认不提供执行器。
- 沙盒只约束显式声明能力的 AI 工具；插件命令处理器直接操作文件或网络不属于本契约。
- 文件检查与写入之间存在常规 TOCTOU 窗口；工作区由管理员控制，不存放对抗性内容。
- 本轮额度保存在事件上下文内，进程重启或任务被取消后不保留；跨轮总量请使用群额度。
- 沙盒拒绝是正常结果，不是系统错误：模型应说明受限部分，而不是反复换参数尝试。
