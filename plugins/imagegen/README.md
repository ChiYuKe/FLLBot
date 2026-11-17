# imagegen 图片生成

默认用 **Pillow 直接画**机器人自己的猫娘形象：银灰色长发、丸子头、呆毛、猫耳、黑旗袍和大尾巴。
不填密钥、不联网就能用，适合表情包、头像和情绪图。管理员也可以另外配置远程图片模型，
画任意画面；不配置时只走本地模式。

## 本地模式（默认）

| 能力 | 说明 |
| --- | --- |
| 表情 | `normal`、`happy`、`angry`、`sad`、`shy`、`sleepy`、`surprised`、`smug`、`love`、`cry`、`blank` |
| 配色 | `night` 夜色、`sakura` 樱花、`rain` 雨、`sunset` 黄昏、`mint` 薄荷、`mono` 黑白 |
| 短文字 | 图片底部气泡，最多 16 字 |
| 种子 | 同一 `seed` 出图稳定，换一个就换一张 |
| 尺寸 | 512 / 768 / 1024，按最长边生成正方形 |

不指定表情和配色时，会从提示词里找关键词：

```text
/画 生气地鼓着腮帮子        → angry + night
/画 雨夜，委屈巴巴          → sad + rain
/画 樱花飘落，好开心        → happy + sakura
/画 猫猫 得意               → smug + night
```

本地模式画的是固定的猫娘角色，**画不了别的角色、写实照片或复杂构图**。
要画别的角色，请让管理员配置下面的远程图片模型；没有配置时机器人会如实说明。

### 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | 开 | 群级开关；关闭后本群不响应 `/画` |
| `provider` | `local` | 本地绘制；改成远程类型才使用 API |
| `size` | `1024x1024` | 本地模式取最长边，最大 1024 |
| `caption_font` | 空 | 图片短文字的字体文件；留空自动找系统中文字体 |
| `cooldown_seconds` | 5 | 同一用户两次生成的最小间隔（群级可覆盖）；远程按次计费时可调高 |

## 远程模式（可选）

| 类型 | 协议 | 常见服务 |
| --- | --- | --- |
| `openai` | `POST {base_url}/images/generations` | OpenAI、智谱 CogView、其他兼容接口 |
| `siliconflow` | 同上（`image_size` 参数） | 硅基流动 Kolors 等 |
| `sdwebui` | `POST {base_url}/sdapi/v1/txt2img` | 本机 Stable Diffusion WebUI |

配置项：服务地址、API Key、模型名称、默认尺寸、追加风格、负面提示词、采样步数、
提示词强度、返回格式、超时、图片大小上限。示例：

| 服务 | 配置 |
| --- | --- |
| 硅基流动 | 类型 `siliconflow`，地址 `https://api.siliconflow.cn/v1`，模型 `Kwai-Kolors/Kolors` |
| OpenAI | 类型 `openai`，地址 `https://api.openai.com/v1`，模型 `gpt-image-1` |
| 智谱 | 类型 `openai`，地址 `https://open.bigmodel.cn/api/paas/v4`，模型 `cogview-4` |
| 本机 SD | 类型 `sdwebui`，地址 `http://127.0.0.1:7860`，启动 WebUI 时加 `--api` |

远程结果会校验格式与尺寸后统一转 PNG，以 base64 直接发送，不落盘，并剥离供应商附带的 EXIF。
远程模式需要宿主机能访问所配置的服务；地址和密钥由管理员掌握，群友不能指定。

## 命令与 AI 工具

| 入口 | 说明 |
| --- | --- |
| `/画 <描述>`（`画图` `绘图` `draw`） | 生成一张图片并发送到当前会话 |
| `imagegen__draw` | 智能体工具，参数见下 |

| 参数 | 适用 | 说明 |
| --- | --- | --- |
| `prompt` | 全部 | 一句中文画面/情绪描述，必填 |
| `expression` | 本地 | 表情枚举；省略时按关键词判断 |
| `palette` | 本地 | 配色枚举；省略时按关键词判断 |
| `caption` | 本地 | 最多 16 字短文字 |
| `seed` | 本地 | 随机种子，换一张时换一个 |
| `size` | 全部 | 「宽x高」；本地取最长边 |
| `negative_prompt` | 远程 | 这次不想出现的内容 |

工具声明 `qq.send`；自动参与（没有被 @ 或唤醒词叫到）时插件拒绝生成，必须明确请求。
未在「智能体 → 允许调用的工具」里勾选「生成图片」时，模型看不到这个工具。

### 直出确认

`imagegen__draw` 在图片确认送达后返回 `silent: true` 与一句简短的 `reply`（“画好啦，已经发到群里”），
宿主可据此直接发送确认、省掉一次模型往返。生成失败、冷却中、未明确请求或图片没有送达时不会返回
`silent`/`reply`，仍由模型如实说明；原有的 `sent`、`provider`、`size`、`bytes` 等字段保持不变。

## 权限与隐私

- 本地模式完全离线，提示词不离开本机；图片也不落盘。
- 远程模式会把提示词发送到管理员配置的服务；不要放令牌、内部地址或他人隐私信息。
- 日志只记录模式、表情/配色、尺寸、字节数和固定原因码，不记录提示词原文或密钥。
- 生成按次计费（远程）或消耗 CPU（本地）；建议保留“同一用户冷却”。

## 常见失败

| 返回 | 含义 |
| --- | --- |
| `imagegen_disabled` | 本群关闭了图片生成 |
| `imagegen_addressed_required` | 自动参与时不会生成，需 @ 或唤醒词明确请求 |
| `imagegen_cooldown` | 冷却中，返回里带 `retry_after` 秒数 |
| `imagegen_not_configured` | 选了远程类型但没填地址/密钥/模型 |
| `imagegen_http_error` | 远程服务返回 4xx/5xx |
| `imagegen_timeout` | 远程生成超时 |
| `imagegen_too_large` | 远程图片超过大小上限 |
| `imagegen_invalid_image` | 远程返回的不是有效图片 |
| `imagegen_not_delivered` | 图片生成成功但 QQ 发送失败或会话已结束 |

## 开发与维护

本地绘制在 `local.py`，远程协议在 `service.py`；模块划分和验证命令见
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。
