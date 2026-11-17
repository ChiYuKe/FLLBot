# 开发说明

## 模块

- `plugin.py`：声明配置、工具与智能体使用说明；负责发送或写入工作区。
- `service.py`：只做 `ImageGrab` 抓取、缩放与编码，返回 bytes 与尺寸；不做 I/O 之外的副作用。

## 安全边界

`screen.capture` 在 `fllbot/sandbox/capabilities.py` 中按 `destructive` 风险处理：
默认 `allow_screen=false`，自动参与被 `write_requires_addressed` 拒绝，开启后仍受
`destructive_requires_superuser` 约束。新增截图相关工具时必须保留这些能力声明，
不要为了“方便”合并进 `process.exec` 或省略 `capabilities`。

截图内容属于隐私资料：不要写入日志、消息追踪或错误信息；返回给模型的只有尺寸、格式、字节数和发送结果。
发送失败或送达不确定时如实返回，不自动重发。

## 交付确认（直出快路径）

`capture_screen` 只有在 `send_media_reply` 返回 `True`（回执确认送达）后才追加
`{"silent": true, "reply": "截好啦，已经发出去"}`；`capture_screen_file` 只有在工作区写入成功后才追加
`{"silent": true, "reply": "截图已经保存到工作区"}`。两者都保留原有 `sent` / `saved` / `path` / 尺寸字段。
宿主在整批工具成功、静默且无失败时直接发送 `reply`，不再请求模型；截屏失败、发送失败、回执缺失或
写入失败时不得返回 `silent` / `reply`。`reply` 不复述尺寸、路径或内部 id。

## 验证

```powershell
.venv/Scripts/python.exe -m fllbot plugin check --builtin
.venv/Scripts/python.exe -m pytest tests/test_screen_plugin.py tests/test_sandbox.py -q
```
