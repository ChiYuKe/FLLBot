# 开发说明

## 模块

- `plugin.py`：声明四个工具与一段智能体使用说明；业务全部委托宿主沙盒。
- 元信息只在 `plugin.toml`；`__init__.py` 只导出 `plugin`。

## 接口约定

工具通过 `ctx.sandbox` 访问宿主提供的 `SandboxFiles` / `SandboxNetwork`，不要自行拼接路径、
打开文件或创建 HTTP 客户端。这样路径围栏、字节上限、DNS/内网检查和重定向校验只有一份实现。

`SandboxError` 继承 `ValueError`，携带安全可展示的 `code`；工具把它转成
`{"error": code, "message": ...}` 返回给模型。不要返回本机绝对路径、DNS 解析结果或原始异常堆栈。

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
