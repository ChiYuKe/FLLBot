# screen

截取运行 FLLBot 的电脑屏幕，元信息见 [plugin.toml](plugin.toml)。**默认关闭**，需要管理员显式开启。

## 工具

| 工具 | 能力 | 说明 |
| --- | --- | --- |
| `screen__capture_screen` | `screen.capture` + `qq.send` | 截取屏幕并把图片发送到当前会话 |
| `screen__capture_screen_file` | `screen.capture` + `fs.write` | 截取屏幕并保存到沙盒工作区 |

注意：截取的是**宿主电脑**的屏幕，不是提问者的设备。截图可能包含桌面上的聊天窗口、令牌、文档等隐私内容。

## 启用条件

必须同时满足：

1. `config.toml` 中 `[sandbox].allow_screen = true`（默认 false）；
2. 保存文件时还需 `[sandbox].allow_write = true`；
3. 群友**明确 @ Bot 或使用唤醒词**提出请求；自动参与不会截屏；
4. `destructive_requires_superuser = true`（默认）时，发起者必须是超级管理员；关闭后仍至少要求群主/管理员；
5. 在「智能体 → 允许调用的工具」中勾选对应工具。

任一条不满足时工具返回 `tool_blocked`（`screen_disabled`、`write_disabled`、`automatic_side_effect`、
`authority_required` 等），不会生成或发送图片。

## 配置

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `all_screens` | false | 是否截取全部显示器 |
| `max_width` | 1600 | 超过后等比缩小 |
| `image_format` | png | `png` 或 `jpeg` |
| `jpeg_quality` | 85 | JPEG 质量 60–95 |

单群可再用 `sandbox_screen` 覆盖全局截屏开关。

## 依赖与边界

- 使用 Pillow 的 `ImageGrab`；无可见桌面的会话（服务、远程无会话）会返回 `screenshot_failed`。
- 单张编码后最多 8 MB；超出时自动退回 JPEG，仍超出则明确失败。
- 这是高风险能力，宿主默认不开放；不要为了“方便”改成自动截屏。
- 沙盒是进程内策略围栏，插件仍以宿主权限运行；只安装可信插件。
