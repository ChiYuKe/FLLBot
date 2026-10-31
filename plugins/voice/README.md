# 语音插件

把文字用 [IndexTTS-2.5](https://github.com/index-tts/index-tts) 念出来，以 QQ 语音消息发送。

依赖本地 TTS 服务：见 `services/tts/README.md`。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/say [情绪] 文本`（`说` / `tts` / `念`） | 合成并发送语音 |
| `/voices`（`音色`） | 列出 TTS 服务里可用的音色 |
| `/ttshealth`（`语音状态`） | 检查服务是否就绪 |

情绪可以写成前缀：`/say [生气] 你再说一遍`、`/say 开心: 好耶`。
可选：开心、生气、难过、害怕、厌恶、低落、惊讶、平静。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | 开 | 群级开关 |
| `backend` | `qwen` | `qwen`：描述固定、稳定；`indextts`：参考音频克隆 |
| `qwen_voice` | `04-奶音` | 试听页里选中的编号（01–10） |
| `tts_voice` | `dongxuelian-soft` | 仅 indextts 后端使用 |
| `with_text` | 开 | 是否同时发文字 |

情绪、强度、语速等表达参数由智能体在每次调用 `voice__speak` 时决定，不需要在配置里填。

## 智能体

插件注册了 `voice__speak` 工具，在「允许调用的工具」里勾选「语音朗读」后，模型就能在用户要求「用语音说」时主动调用。

## 音色

插件不带任何角色音色，只用参考音频做 zero-shot 克隆。把东雪莲的干声放到
`services/tts/voices/dongxuelian.wav` 即可；没有该文件时回落到 `default.wav`。

声音克隆请自行确认授权范围，公开场景建议标注 AI 合成。


## 插件包维护

元信息以 [plugin.toml](plugin.toml) 为准，开发约定见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)，更新记录见 [CHANGELOG.md](CHANGELOG.md)。
