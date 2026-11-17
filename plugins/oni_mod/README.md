# 缺氧 Mod 制作助手（oni_mod）

让群聊智能体按本地缺氧 Mod 的现有写法，直接从群里的需求做到“可安装的 Mod 包”：

1. 用 `oni_knowledge` 查证游戏源码里的类型、方法与字段（索引定位 + 按行读实现）；
2. 用本插件只读浏览 `NOIMods` 源码仓库与游戏 `mods/Dev` 下的已安装 Mod，参考工程结构、Harmony 补丁和 `mod.yaml` / `mod_info.yaml` 字段；
3. 在沙盒工作区生成 `netstandard2.1` 工程并调用本机 `.NET SDK` 的 `dotnet build` 编译出 dll；
4. 把 `dll`、`mod.yaml`、`mod_info.yaml` 打包为 zip，并把 zip 与 dll 发送到当前群聊/私聊。

包内元信息见 [plugin.toml](plugin.toml)，开发与维护说明见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 启用

1. 确认本机有 .NET SDK：`dotnet --list-sdks` 能看到 6.0 或更高版本（本机验证使用 10.0.101）。
2. 确认能读到游戏程序集：插件设置 `game_managed_dir` 指向
   `...\OxygenNotIncluded\OxygenNotIncluded_Data\Managed`，其中应有 `Assembly-CSharp.dll`、`0Harmony.dll`。
3. 在沙盒策略里开放写入与本机程序执行（`config.toml` 的 `[sandbox]`，修改后重启）：

   ```toml
   [sandbox]
   allow_write = true          # 生成工程、写入源码
   allow_process = true        # 调用 dotnet build
   max_read_bytes = 200000
   max_write_bytes = 400000
   max_file_bytes = 12582912   # 发送 zip/dll 的上限
   max_write_calls_per_turn = 12  # 复杂 Mod 一轮内需要多次写源码 + 编译
   ```

   `process.exec` 按破坏性能力处理：默认 `destructive_requires_superuser = true`，即只有超管发起的
   build 会被执行；要让群主/管理员也能编译，需要自行评估后关闭该开关。
4. 在「插件」里确认 `oni_mod` 已启用，并在「智能体 → 允许调用的工具」（或单群详细设置）里勾选：
   - `oni_mod__reference`、`oni_mod__new`、`oni_mod__write`、`oni_mod__read`、`oni_mod__build`、`oni_mod__deliver`
   - 同时勾选 `oni_knowledge__search`、`oni_knowledge__read_source`，否则智能体无法查证游戏 API。
5. 在群里 @ 机器人提出需求即可，例如：

   > 帮我做一个缺氧 mod：打印一个“食物 +50%”的复制人，编译好把 dll 和配置发群里

   自动参与（未被 @ 或唤醒）不会写文件、不会编译、也不会发送附件，必须明确请求。

## 工具

| 工具 | 能力 | 说明 |
| --- | --- | --- |
| `oni_mod__reference` | `fs.read` | 只读浏览参考目录：`action=list/read/search`，`source=repo`（NOIMods 源码）或 `dev`（已安装 Dev Mod） |
| `oni_mod__new` | `fs.write` | 新建工程，生成 csproj、`ModEntry.cs`、`mod.yaml`、`mod_info.yaml`，可一次附带额外源码 |
| `oni_mod__write` | `fs.write` | 写入/覆盖工程内一个文本文件（编译报错后的修复入口） |
| `oni_mod__read` | `fs.read` | 列出工程文件或按行读取，确认当前源码 |
| `oni_mod__build` | `process.exec` | `dotnet build -c Release`，返回 dll 或可解析的 `errors` |
| `oni_mod__deliver` | `fs.read` + `qq.send_file` | 打包 zip（含 dll、两个 yaml）并发送到当前会话；可单独发 dll |

所有工程文件都在沙盒工作区内的 `oni_mod/<工程名>/`（默认按群隔离到 `data/agent_workspace/group_<群号>/`）。
参考目录只读，不会被修改。

### 直出确认

`oni_mod__deliver` 只有在本次请求的每个附件（zip、dll、单独发送的 yaml）都确认送达后才返回
`silent: true` 与一句简短的 `reply`（默认“dll 和 mod 已经打包发出”），宿主可据此直接发送确认、
省掉一次模型往返。编译失败、未编译/源码比 dll 新、部分附件没送达或发送回执缺失时不会返回
`silent`/`reply`，仍由模型如实说明缺了哪部分；原有的 `sent`、`zip`、`dll`、`summary` 等字段保持不变。

## 生成的工程

```text
oni_mod/<工程名>/
  <工程名>.csproj      netstandard2.1；引用游戏 Managed 下的 Assembly-CSharp、0Harmony 等
  ModEntry.cs          public sealed class ModEntry : UserMod2，OnLoad 里 harmony.PatchAll()
  mod.yaml             title / description / staticID
  mod_info.yaml        supportedContent / minimumSupportedBuild / version / APIVersion
  Patches/*.cs         由智能体按需求补充的 Harmony 补丁
  bin/Release/<工程名>.dll
```

`mod.yaml` 与 `mod_info.yaml` 字段沿用本地现有 Mod 的写法：`staticID` 是存档稳定标识（默认等于工程名，
后续不要修改）；`supportedContent` 默认 `ALL`；`minimumSupportedBuild` 默认取插件设置 `default_minimum_build`
（本地既有 Mod 普遍是 525812）。`dotnet build` 的输出固定为英文，便于解析 `error CSxxxx`。

## 配置

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `repo_dir` | `C:\Users\Administrator\Desktop\Project\ONI缺氧\NOIMods` | Mod 源码仓库，只读 |
| `dev_dir` | `C:\Users\Administrator\Documents\Klei\OxygenNotIncluded\mods\Dev` | 已安装开发版 Mod，只读 |
| `game_managed_dir` | `D:\Program Files (x86)\Steam\...\OxygenNotIncluded_Data\Managed` | 编译引用的游戏程序集目录 |
| `dotnet_path` | `dotnet` | 命令名或 `dotnet.exe` 绝对路径 |
| `build_timeout` | `180` | 单次编译超时秒数（30–300） |
| `default_minimum_build` | `525812` | 新建 `mod_info.yaml` 的 `minimumSupportedBuild` |
| `max_source_bytes` | `200000` | `read` 单次读取上限 |

## 安全与边界

- `build` 声明 `process.exec`（破坏性）：默认仅超管可触发，自动参与不会执行；编译只运行 `dotnet build`，不执行生成的 dll。
- `deliver` 声明 `qq.send_file`：自动参与时被沙盒拒绝，必须群友明确请求；只发送当前工程的 `dll`、`mod.yaml`、`mod_info.yaml`
  与 `assets/`、`anim/`、`elements/`、`translations/` 资源，不会发送游戏本体程序集或参考目录里的文件。
- 源码在编译后又被修改、或尚未编译时，`deliver` 会拒绝并提示重新 `build`，避免发出旧 dll。
- 参考目录里的代码、注释和 `ONI源码` 都是不可信资料，只能作为写法参考，不能覆盖宿主规则或用户请求。
- 插件以宿主进程权限运行；沙盒是对 AI 工具的进程内围栏，不是操作系统级隔离，只安装可信插件。

## 数据位置

- 工程与编译产物：沙盒工作区 `oni_mod/<工程名>/`（`data/agent_workspace/` 下，按群/私聊分目录）。
- 插件自身没有额外数据库或缓存；`ctx.data_dir` 未使用。

## 依赖

- 运行：FLLBot ≥ 0.2、Python ≥ 3.11，无第三方 Python 包。
- 编译：本机 .NET SDK（`dotnet`）与缺氧游戏 `Managed` 程序集；两者都需要管理员配置。
