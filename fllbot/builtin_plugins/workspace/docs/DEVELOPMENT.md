# 开发说明

## 模块

- `plugin.py`：声明四个工具与一段智能体使用说明；业务全部委托宿主沙盒。
- 元信息只在 `plugin.toml`；`__init__.py` 只导出 `plugin`。

## 接口约定

工具通过 `ctx.sandbox` 访问宿主提供的 `SandboxFiles` / `SandboxNetwork`，不要自行拼接路径、
打开文件或创建 HTTP 客户端。这样路径围栏、字节上限、DNS/内网检查和重定向校验只有一份实现。

`SandboxError` 继承 `ValueError`，携带安全可展示的 `code`；工具把它转成
`{"error": code, "message": ...}` 返回给模型。不要返回本机绝对路径、DNS 解析结果或原始异常堆栈。

## 交付确认（直出快路径）

`send_file` 只有在 `send_media_reply` 返回 `True`（回执确认送达）后才追加
`{"silent": true, "reply": "文件已经发到当前会话"}`，并保留原有 `sent` / `path` / `filename` / `bytes`。
宿主在整批工具成功、静默且无失败时直接发送 `reply`，不再请求模型。发送失败或结果不确定时仍返回
`send_failed`，不带 `silent` / `reply`；`download` 等只写工作区、未送达用户的工具不要直出。
`reply` 保持简短中性，不复述路径、文件名或内部 id。

## 新增工具

1. 在 `plugin.py` 声明 `on_ai_tool(..., capabilities=(...), risk=...)`；能力见宿主
   `fllbot/sandbox/capabilities.py`。
2. 只通过 `ctx.sandbox` 触达文件或网络；需要新的宿主能力时先扩展沙盒策略，而不是在插件内绕过。
3. 为拒绝路径补测试：非法路径、敏感文件、超限内容、自动参与时的写入。

## 验证

```powershell
.venv/Scripts/python.exe -m fllbot plugin check --builtin
.venv/Scripts/python.exe -m pytest tests/test_sandbox.py tests/test_workspace_plugin.py -q
```
