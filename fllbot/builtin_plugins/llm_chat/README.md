# llm_chat

大模型多轮对话：私聊直接说话，群里 @ 机器人后提问，支持人格与上下文管理。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/chat` | 直接对话（需 `superuser` 或群管理员） |
| `/对话` | 中文别名 |
| `/chat clear` | 清空当前会话的上下文 |
| `/chat persona` | 查看当前人格和可用人格 |
| `/chat persona <名称>` | 切换本群人格（仅群聊） |

## 触发方式

- **私聊**：直接发消息即对话（可由 `llm.enable_private` 关闭）
- **群聊**：`@机器人 你的问题`；`llm.trigger` 设为 `prefix` 时用 `ai 你的问题`
- 冷却时间由 `llm.cooldown_seconds` 控制，默认 3 秒

## 配置

在 `config.toml` 的 `[llm]` 段：

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 总开关 |
| `base_url` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `api_key` | 空 | API Key，也可用环境变量 `FLLBOT_LLM__API_KEY` |
| `model` | `deepseek-chat` | 模型名 |
| `enable_private` / `enable_group` | `true` | 场景开关 |
| `persona` | `fllbot` | 默认人格 |
| `max_context_messages` | `16` | 上下文保留的消息条数 |

## 说明

- 未配置 `api_key` 时插件仍会加载，但不会响应对话
- 长回复按 `llm.chunk_size` 分段发送
- 人格文件放在 `personas/*.md`，内置 `fllbot` 与 `default`
