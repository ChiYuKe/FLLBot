# oni_mod 开发与维护

插件包名 `oni_mod`，元信息只在 [plugin.toml](../plugin.toml)。本文说明模块划分、编译流程、错误契约与验证方式。

## 模块

| 文件 | 职责 |
| --- | --- |
| `plugin.py` | 读取清单、声明配置与 Agent 提示词片段、注册 6 个 AI 工具，负责 `ctx` 到服务的适配 |
| `service.py` | 纯业务：名称/路径校验、工程模板、`dotnet build` 调用与输出解析、构建标记、zip 打包、参考目录浏览 |
| `__init__.py` | 只导出 `plugin` |
| `README.md` / `CHANGELOG.md` | 使用说明与版本记录 |

包内只从 `fllbot.plugin` 导入宿主接口（`Context`、`Plugin`、`MessageSegment`、`send_media_reply`），
不导入宿主内部模块；测试放在仓库根目录 `tests/`。

## 请求流程

```text
oni_knowledge__search / read_source   查证游戏 API（另一插件，需单独授权）
        │
oni_mod__reference                   只读查看 NOIMods / Dev 中同类 Mod 写法
        │
oni_mod__new                         生成 csproj、ModEntry.cs、mod.yaml、mod_info.yaml
oni_mod__write                       写入 / 修正补丁源码
        │
oni_mod__build                       dotnet build -c Release → bin/Release/<name>.dll
        │  errors → 修改源码 → 再次 build
oni_mod__deliver                     zip（dll + 两个 yaml + 资源）+ dll → 当前会话
```

`plugin.agent_instruction("oni_mod_authoring", ...)` 只在 5 个创作类工具都被授权时注入；
`plugin.agent_instruction("oni_mod_delivery", ...)` 只在 `deliver` 被授权时注入。

## 沙盒与路径

- 工程根目录是 `ctx.sandbox.files.root` 下的 `oni_mod/<工程名>/`，随 `workspace_scope` 按群/私聊隔离。
- 工具声明的参数路径（`path`）交给宿主沙盒做工作区相对路径校验；服务层再用自己的
  `validate_relative` / `_inside` 复核一次，拒绝绝对路径、盘符、`..`、`bin` / `obj` 与二进制扩展名。
- 文本写入走 `ctx.sandbox.files.write_text`，受 `max_write_bytes` 限制；读取走 `read_text` / `read_bytes`，
  受 `max_read_bytes` / `max_file_bytes` 限制。
- 编译产物由 `dotnet` 进程直接写盘，这是 `process.exec` 能力的固有部分；工具按 destructive 处理，
  并在编译前后检查 `ctx.data_current`。

## 编译

`service.run_build`：

1. 校验 `<工程名>.csproj` 存在；`resolve_game_dir` 找到含 `Assembly-CSharp.dll` 的 Managed 目录，
   `resolve_dotnet` 找到 dotnet（命令、绝对路径或 `%ProgramFiles%\dotnet`）。
2. 以 `cwd=工程目录` 执行：
   `dotnet build <csproj> -c Release --nologo -v:m -m:1 -p:GameManagedDir=<Managed> [--no-incremental]`。
   环境固定 `DOTNET_CLI_UI_LANGUAGE=en`，保证 `error CSxxxx` 可用且输出编码稳定。
   同一工程用模块级 `asyncio.Lock` 串行编译，避免两个 build 争抢 `obj`/`bin` 文件锁。
3. 超时（`build_timeout`）或任务取消时用 `taskkill /F /T` 结束整棵进程树；取消会继续向外传播。
4. 解析 `error/warning CSxxxx: 消息` 与无位置信息的 `error MSBxxxx`，去重后最多返回 20 条。
5. 成功条件：退出码为 0、`bin/Release/<工程名>.dll` 存在且修改时间不早于本次编译开始。
6. 成功后写 `bin/Release/.oni_mod_build.json`（`built_at`、dll 字节数），供 `deliver` 判断 dll 是否新鲜；
   `build_state` 还会比较工程内源码的最新修改时间，源码比 dll 新时要求重新编译。

## 错误契约

工具失败返回 `{"error": <code>, "message": ...}`，不抛异常给模型。常用 code：

| code | 含义 |
| --- | --- |
| `project_not_found` / `project_exists` / `project_incomplete` | 工程不存在、已存在、缺 csproj |
| `not_built` / `artifact_missing` | 未编译 / 源码比 dll 新 / 缺 yaml |
| `package_too_large` | zip 原始内容超过 `max_file_bytes` |
| `context_expired` | 会话在编译或发送期间失效 |
| `send_failed` | 已读取文件但未确认送达 |
| 沙盒错误码（`write_disabled`、`process_disabled`、`tool_blocked`…） | 由宿主在执行前返回，回调不会运行 |

`build` 失败时返回 `ok=false`、`errors`、`output_tail` 与 `hint`；成功时返回 `dll`、`dll_bytes`、`duration_seconds`。

## 工程模板

- csproj：`netstandard2.1`、`AppendTargetFrameworkToOutputPath=false`、`OutputPath=bin\Release\`、
  游戏引用用 `$(GameManagedDir)`，只生成 Managed 目录里真实存在的程序集引用。
- `ModEntry.cs`：`UserMod2` 入口，`base.OnLoad(harmony)` 后 `harmony.PatchAll()`。
- `mod.yaml`：`title` / `description` / `staticID`，字符串用 JSON 转义（YAML 双引号兼容）。
- `mod_info.yaml`：`supportedContent` / `minimumSupportedBuild` / `version` / `APIVersion: 2`。

`supportedContent` 在游戏源码里已标记为 deprecated（`KMod/Mod.cs`），但本地全部 Mod 仍在使用；
如要改用 `requiredDlcIds` / `forbiddenDlcIds`，只需改 `render_mod_info` 与 README，不影响编译。

## zip 内容

`service.package_entries` 收集：`mod.yaml`、`mod_info.yaml`、`bin/Release/<name>.dll` 与
`assets/`、`anim/`、`elements/`、`translations/` 下的文件；zip 内层目录为 `<工程名>/`，
解压到 `mods/Dev` 即得到游戏可识别的目录结构。`deliver` 默认发送 zip 与 dll；`separate_yaml=true` 时
再单独发送两个 yaml。

## 验证

```powershell
.\.venv\Scripts\python.exe -m fllbot plugin check --name oni_mod
.\.venv\Scripts\python.exe -m ruff check plugins/oni_mod
.\.venv\Scripts\python.exe -m pytest -q tests/test_oni_mod_plugin.py
```

测试覆盖：模板生成与 yaml 字段、路径校验、参考目录浏览、`dotnet build` 真实编译、构建标记与
源码过期判断、zip 结构，以及在宿主里经 `tool_executor` 调用 `new` / `build` / `deliver` 的沙盒裁决
（需要本机 SDK 与游戏 Managed 目录；缺失时相关用例会跳过）。编译测试首次运行会触发 NuGet/SDK 初始化，
通常需要几秒到十几秒。

修改接口时同步更新本文与 README；发布时更新 `plugin.toml` 与 `CHANGELOG.md`。
