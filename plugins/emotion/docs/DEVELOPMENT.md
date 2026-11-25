# emotion 开发与维护

## 模块划分

| 文件 | 职责 |
| --- | --- |
| `plugin.py` | 包头实例、配置声明、命令/AI 工具、生命周期、周期任务与私聊互聊处理器 |
| `engine.py` | 情绪状态、群活跃内存、选人、限额、发送、提示词调用与互聊窗口 |
| `mood.py` | 心情数据结构、标签/语气、确定性情绪信号 |
| `store.py` | SQLite：情绪、悄悄话记录、用户关闭设置 |
| `whisper.py` | 生成提示词、输出清洗、本地备用短句 |

插件只从 `fllbot.plugin` 导入宿主接口；宿主对象上的 `bot.chat`、`bot.permissions`、`bot.manager`、
`bot.storage` 是运行期协作入口，不导入宿主内部模块。

## 状态模型

`Mood` 有两个连续维度：

- `valence`：-1（很差）到 1（很好），基线 0.12；
- `energy`：0（困倦）到 1（精神），基线 0.55。

`mood_decay_minutes` 是回落到基线的半衰期。`Mood.label` 由两个维度推导出
困倦/炸毛/低落/兴奋/不爽/开心/来劲/平静，`tone` 再给模型一句语气描述。

确定性信号在 `mood.judge()` 中：夸奖、凶人、笑声、重复标点、被 @、复读、深夜。每条消息还会加
少量活跃度，避免情绪只在被点名时才变化。戳一戳 +0.05，`/哄哄` +0.12。

## 活跃度与选人

`engine._activity` 是按 `(self_id, group_id)` 分组的 `deque`，最多 120 条；不持久化，重启清空。
每条只保留 QQ、昵称、最多 160 字文本、时间、消息 ID 和“是否 @ 机器人”。

用户是否“和机器人说过话”单独记录在 `interactions` 表：群里 @ 机器人、用唤醒词开头、发命令，
或与机器人私聊都会更新 `last_at`。私聊记录使用 `group_id=0`，因此对同一账号的所有群生效。
`only_interacted` 默认开启，选人时只保留 `interaction_window_days`（默认 30 天）内互动过的用户；
关闭后恢复为任意活跃群友。

轮询每 60 秒执行一次（`initial_delay=90`）。`tick()` 在同一个 `asyncio.Lock` 内：

1. 推进所有情绪的时间衰减并定期落盘；
2. 按最近活跃排序，最多检查 50 个群（与内存活跃群上限一致）；
3. 依次检查插件/群开关、`allow_private`、安静时段、每群间隔、每群和全局每日额度；
4. 按 `probability` 掷骰子；
5. 在 `active_window_minutes` 内过滤最近发言，要求达到 `min_messages` 条；
6. 默认只保留最近 `interaction_window_days` 天内和机器人说过话的人；关闭 `only_interacted` 可跳过；
7. 按发言条数和新鲜度加权选人，排除自己、关闭悄悄话的用户、个人冷却中的用户；
8. 尽量不连续选同一个人。

## 生成与发送

`_model_line()` 使用宿主当前群的人格构造 system 消息，再把 `whisper.build_prompt()` 作为 user 消息，
通过 `bot.chat.complete` 单轮生成（最多 350 tokens，45 秒超时，不写群历史）。失败、空输出、
超长或包含禁用内容时回退到 `whisper.local_line()`。

`_deliver()` 调用 `connection.send_private_msg()`：

- `delivery="temp"`：带 `group_id`，NapCat 走群临时会话；
- `delivery="private"`：普通私聊，仅对好友有效。

成功后写入 `whispers`，并可选写入宿主私聊历史（`scene="private"`、`role="assistant"`），
让群友从临时会话回复时能接上前文；同时打开私聊互聊窗口。失败只记录错误类型并进入冷却，不自动重试。

## 私聊互聊窗口

`_deliver()` 发送成功后，按 Bot、用户、来源群和投递方式保存窗口开始时间。
`recent_sent_targets()` 在引擎初始化时从 `whispers` 表恢复近期窗口。
`session_started()` 按全局 `chat_window_minutes` 筛选有效窗口，`opt_out()` 会移除窗口。

`plugin.py` 的 `whisper_session_chat` 处理器（priority=85，抢在智能体的 90 之前）：

1. 处理私聊文字、图片和表情，也接受文字和图片混发。命令及其他媒体交给后续处理器；
2. 在有效窗口内处理。临时会话优先使用事件中的来源群号，缺失时只补齐唯一可确认的来源群；
3. `engine.session_reply()` 走 `conversations.turn` 领取轮次，用 `chat.build_messages`
   （`history_since=窗口开始`，附 `SESSION_CHAT_RULES`）加 `chat.complete` 快聊。
   图片由宿主判断是否作为模型输入，模型失败静默收尾；
4. 通过 `deliveries.send_text(require_ai=False)` 投递（分段/长回复/投递记录与智能体一致），
   输入投影和助手消息落库由宿主统一完成；成功后 `ctx.stop()`，智能体不再处理同一条消息。

## 存储

## 存储

`data/plugins/emotion/emotion.db`，WAL 模式：

- `moods(self_id, group_id, valence, energy, updated_at, last_reason, last_event_at)`
- `whispers(id, self_id, group_id, user_id, nickname, kind, content, created_at, status, error)`
- `optouts(self_id, user_id, created_at)`
- `interactions(self_id, group_id, user_id, last_at)`，`group_id=0` 表示私聊互动

发送记录保留 30 天，互动记录保留 180 天。情绪随轮询落盘：`_maybe_flush()` 最多每 10 秒写一次，实际通常每分钟一次；卸载时强制落盘。
写入失败只记 debug 日志，不阻塞消息处理。

## 权限顺序

主动发送前必须同时满足：插件全局/群开关、群启用、`permission.allow_private`、配置 `enabled`、
配置 `whisper_enabled`。`emotion__send_whisper` 额外要求当前轮被明确叫到（`trigger_decision.addressed`），
并有 10 分钟工具冷却；它只能发给当前发言人，不能指定其他 QQ。

## 资源清理

`EmotionPlugin.on_load()` 创建 `MoodStore` 与 `EmotionEngine`；`on_unload()` 先落盘再关闭 SQLite 连接。
周期任务由宿主在卸载时取消。热重载时新版本先初始化成功，旧版本随后关闭，互不共享引擎。

## 验证

```powershell
.venv/Scripts/python.exe -m ruff check plugins/emotion tests/test_emotion_plugin.py
.venv/Scripts/python.exe -m fllbot plugin check --root . --name emotion
.venv/Scripts/python.exe -m pytest -q tests/test_emotion_plugin.py
.\scripts\verify.ps1
```

离线测试使用 `tests/helpers.py` 的 `FakeConnection`，通过 `engine.clock` 注入固定时间，避免依赖真实时钟。
新增行为时优先补：情绪信号、互动记录、选人排除、额度/间隔、安静时段、用户关闭、临时会话发送参数、
互聊窗口的开启/过期/恢复与快聊上下文。
