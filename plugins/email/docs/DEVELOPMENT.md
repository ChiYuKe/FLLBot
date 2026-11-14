# email 开发说明

- `plugin.toml`：元信息唯一来源；插件名固定为 `email`，工具 ID 为 `email__send_email`。
- `__init__.py`：只导出 `plugin`。
- `plugin.py`：声明配置、Agent 指令和 `send_email` 工具，读取 `ctx.settings` 并调用 `service.py`。
- `service.py`：纯函数与 SMTP 实现。`parse_recipients` 负责 QQ 号/邮箱校验与去重，
  `validate_config` 校验发件配置，`send_email` 用标准库 `smtplib` 发送纯文本邮件。
- `README.md`：安装、配置、工具用法与常见问题；`CHANGELOG.md` 记录发布变化。

约定：

- 网络发送放在 `asyncio.to_thread` 中，避免阻塞事件循环；超时覆盖连接、登录和发送。
- 错误统一为 `EmailError(code, message)`，`code` 是固定英文原因码，`message` 可安全展示给模型和群友。
- 不记录授权码、邮件正文或完整 SMTP 异常；插件日志只写原因码。
- 新增副作用能力时同步在 `fllbot/sandbox/capabilities.py` 声明，并补拒绝路径测试。
- 收件人限制、正文上限等以插件配置为准，工具 Schema 只做基础类型与长度提示。

离线检查：

```powershell
.venv/Scripts/python.exe -m fllbot plugin check --name email
```
