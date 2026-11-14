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

## 验证

```powershell
.venv/Scripts/python.exe -m fllbot plugin check --builtin
.venv/Scripts/python.exe -m pytest tests/test_screen_plugin.py tests/test_sandbox.py -q
```
