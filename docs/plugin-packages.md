# FLLBot 插件包开发规范

适用项目：**FLLBot**。本规范描述消息处理、命令、周期任务与 AI 工具插件，和 QQBot 桌面控制台插件协议分别维护。

## 一文件夹一插件包

```text
plugins/
  voice/                        语音插件，替代原 voice.py + voice.md
  web/                          联网插件，替代原 web.py + web.md
  <name>/
    plugin.toml                 必需：包头与稳定标识的唯一来源
    __init__.py                 必需：只导出 plugin
    plugin.py                   必需：插件声明、模块装配与生命周期
    handlers.py                 按需：命令、事件与 AI 工具注册
    settings.py                 按需：配置字段声明
    service.py                  按需：复杂业务或外部服务客户端
    README.md                   必需：使用说明
    CHANGELOG.md                必需：版本与兼容变化
    docs/
      DEVELOPMENT.md            必需：开发与维护说明
    assets/                     按需：静态资源，不放运行缓存
    tests/                      按需：离线测试与合成测试数据
```

插件目录的直接子文件夹就是安装单元，不再嵌套“分类/插件”目录。文件夹名称与清单 `name` 一致，
以小写字母开头，只含小写字母、数字和下划线，最长 64 字符，不能是 Windows 保留名。
复制、安装、更新、移除均以完整目录包为单位，宿主源码不放用户插件实现。
`fllbot/builtin_plugins/basic/` 与 `agent/` 使用同一包结构，只是跟随 FLLBot 一起发行。

## 包头与稳定标识

```toml
schema_version = 1
api_version = 1
name = "hello"
version = "0.1.0"
description = "打招呼插件"
author = "你的名字"
category = "plugin"
```

`schema_version` 是包结构版本，`api_version` 是宿主插件接口版本，当前都为 1。
`name`、`version`、`description` 必填，`author` 建议填写；版本采用 `0.1.0` 三段形式，可带预发布或构建后缀。
`category` 延续现有 `plugin` / `tool` 分类，不改变管理界面的权限语义。
**插件是管理单位，AI 工具是插件显式开放的能力**；提供工具不要求把整个插件改为 tool 分类。

包内通过 `Plugin.from_manifest(__file__)` 创建实例，避免代码和包头重复填写版本、作者等信息。
加载器会校验两者是否一致，缺文件、格式错误和重复插件名会进入加载失败列表。
不同文件夹不能静默覆盖同名运行插件，重载只替换同一来源。
稳定 `name` 关联现有配置、权限、群覆盖、工具标识与数据，不要为了移动目录修改它。

## 快速开发

在 FLLBot 项目根目录执行：

```powershell
.venv/Scripts/python.exe -m fllbot plugin new hello --description "打招呼插件" --author "你的名字"
.venv/Scripts/python.exe -m fllbot plugin new helper --template tool --description "示例 AI 工具"
.venv/Scripts/python.exe -m fllbot plugin check --name hello
.venv/Scripts/python.exe -m fllbot plugin list
```

默认 `command` 模板包含配置字段与命令；`tool` 模板额外生成显式的 `on_ai_tool` 示例，仍需按权限规则授权模型使用。
`--root` 指定宿主根目录，`--directory` 指定相对于 root 的插件目录，默认 `plugins`。
`plugin check --builtin` 同时检查用户与内置包。检查只解析 TOML、文件结构和 Python 语法，不执行插件代码、不连接外部服务；
运行时能否成功初始化仍需实际加载验证。
`fllbot init` 也生成 `plugins/hello/` 完整包，已有插件和配置不会被覆盖。

源模板位于 `fllbot/plugin/templates/`，随安装包分发。修改源模板影响后续生成的插件，不覆盖已有插件。

## 文件职责与文件头

`__init__.py` 只保留 `from .plugin import plugin` 和导出列表；`plugin.py` 读取清单并连接处理器与配置。
小插件可以把现有逻辑留在 plugin.py；复杂功能按职责拆分，避免每个文件都引入相同配置或循环导入。
新建模板默认提供 handlers.py 与 settings.py，包内使用相对导入。

Python 模块顶部使用文档字符串，例如：

```python
"""Plugin: hello | Role: command handlers | Metadata: plugin.toml."""
```

文件头写稳定标识、职责和清单位置；版本与作者只维护在 plugin.toml。
README 开头标明包名并链接包头，正文包含用途、安装使用、命令与工具、配置、权限、依赖和数据位置。
`docs/DEVELOPMENT.md` 面向后续维护者，说明模块划分、接口、资源清理和验证命令；CHANGELOG 记录每次版本变化。

## 生命周期与热重载

1. 扫描发现直接子目录；声明标准包时先校验清单与必要文档。
2. 在独立模块命名空间中导入，导入阶段只声明配置、事件处理器和工具。
3. 加载配置并执行 `on_load(bot)`，成功后才替换工作中的旧插件并注册处理器、周期任务。
4. 卸载或重载时取消并等待周期任务，注销处理器，执行 `on_unload(bot)` 和逆序清理回调。
5. 新源码导入、校验或初始化失败时恢复旧模块；旧处理器继续工作，失败原因在管理 API 和日志中可见。

`plugin.on_cleanup(callback)` 支持同步或异步函数。创建 HTTP 客户端、连接、后台资源时立即登记关闭方法，
清理函数应允许重复调用。避免在 on_load 中修改其他插件或全局共享对象，宿主无法自动回滚任意外部副作用。
周期任务使用 `@plugin.on_loop(...)`，不要在导入时自行创建无限任务。

热重载监测用户包中的 Python、Markdown、TOML、JSON、YAML 文件，能识别新增、删除、文档与包头变化，
而不只比较目录中最大的修改时间。内置插件通过管理 API 手动重载或重启更新。
辅助模块缓存会一并刷新，语法错误或初始化失败不会卸载工作中的旧版。

## 配置、运行数据与外部服务

配置通过 `plugin.setting()` 声明，使用 `ctx.settings` 读取，全局/群覆盖与密钥掩码复用宿主实现。
结构化数据使用现有 `ctx.db`，文件写入 `ctx.data_dir` / `plugin.data_dir`，实际位置为 `data/plugins/<name>/`。
源码包不包含本机配置、数据库、令牌、模型文件、语音缓存或日志。

`services/tts`、`services/qwen-tts` 是拥有独立环境和模型的外部服务，voice 插件只负责客户端与命令。
不要为满足“一文件夹一插件”把大模型或虚拟环境搬进插件包。外部依赖与启动方式写进包内 README，宿主不自动执行依赖安装命令。

## 迁移与验证

已有 voice、web、basic、agent 已迁移，命令、配置键与工具名称保持原值，已有数据无需迁移。
旧 `.py + .md` 插件及无 plugin.toml 的目录插件暂时兼容；离线标准检查会要求将它们整理为目录包。
更新正在运行的 FLLBot 时先重启一次，以启用新的加载器并清除旧入口；后续按标准热重载即可。

每次修改验证命令派发、工具授权、群覆盖、配置保存、重载失败回滚与资源释放。
运行 `scripts/verify.ps1` 统一检查架构边界、插件包、静态代码和自动测试。
