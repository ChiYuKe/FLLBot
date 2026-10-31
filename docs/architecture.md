# FLLBot 项目架构

此文档描述 **FLLBot 机器人后端**。入口是 `python -m fllbot`，不包含 QQBot 桌面宿主的业务实现。

```mermaid
flowchart LR
    QQ[NapCat / OneBot 11] --> Protocol[onebot 协议与连接]
    Protocol --> App[app.py 应用装配与事件入口]
    App --> Dispatcher[dispatcher 规则 / 权限 / 派发]
    Dispatcher --> Packages[独立插件包]
    Packages --> SDK[plugin SDK / Context]
    SDK --> Storage[storage 持久化]
    SDK --> LLM[llm 模型与会话]
    UI[ui 管理界面] --> API[api 鉴权与管理接口]
    API --> App
    Packages --> Services[外部服务：TTS / 搜索等]
```

## 顶层目录

| 路径 | 职责 | 开发约定 |
| --- | --- | --- |
| `fllbot/` | 可安装、可打包的宿主源码 | 核心模块不导入用户插件的具体实现 |
| `plugins/<name>/` | 用户插件包 | 一文件夹一插件，包头、代码、用户和开发文档都在包内 |
| `fllbot/builtin_plugins/<name>/` | 随发行版提供的插件包 | 与用户插件共用清单与生命周期规范 |
| `services/` | 有独立依赖环境的外部服务 | 模型、独立虚拟环境与服务进程不混入插件目录 |
| `personas/` | 用户维护的提示词与人设 | 与宿主默认 personas 分开 |
| `tests/` | 离线回归测试与合成数据 | 用模拟连接、HTTP transport 与临时数据库隔离真实 QQ 和服务 |
| `scripts/` | 项目验证和架构工具 | 不放运行时业务实现 |
| `tools/` | 开发辅助与探测工具 | 与机器人运行入口分离 |
| `docs/` | 架构、协议、部署和开发规范 | 接口调整时同步维护 |
| `data/`、`logs/` | 数据库、令牌、插件文件与日志 | 本机生成，不提交、不写回插件源码 |

## 核心职责

| 模块 | 职责 |
| --- | --- |
| `cli.py` / `__main__.py` | 启动、初始化、配置检查与插件包开发命令 |
| `app.py` | 组装共享服务、统一生命周期、接收并交付事件；不放具体用户命令 |
| `config.py` | 默认配置、TOML、环境变量与校验 |
| `onebot/` | 消息模型、消息段、连接与协议交互，不反向调用插件或管理 API |
| `dispatcher.py` / `rules.py` | 处理器派发、规则、并发、超时与冷却 |
| `permission.py` / `policy.py` | 开关、用户和群权限、策略解析；插件复用而非重复实现 |
| `storage/` | SQLite 与迁移、消息和配置持久化，不导入 UI、插件或应用入口 |
| `events.py` / `logging_setup.py` | 事件分发、去重、日志与观测 |
| `llm/` | 模型客户端、会话、人设、输出与转发消息适配；不依赖具体插件或 API 页面 |
| `api/` | 鉴权、参数校验、管理与控制台接口，调用已装配的宿主能力 |
| `ui/` / `webui.py` | 界面静态资源与页面交付，不承担插件业务执行 |

## 插件框架内部

```text
fllbot/plugin/
  __init__.py       稳定公开 SDK 导出
  base.py           Plugin、处理器、周期任务、AI 工具、资源清理
  context.py        当前事件上下文、回复、配置、数据与宿主能力
  settings.py       声明式字段、全局/群覆盖、持久化与密钥掩码
  manifest.py       plugin.toml 包头与目录契约，不执行源码
  imports.py        隔离命名、源码导入、子模块缓存与导入回滚
  loader.py         扫描、注册、重载事务、周期任务与运行态管理
  scaffold.py       插件包生成与离线语法检查
  templates/        随安装包分发的模板源文件
```

宿主负责共性的配置、权限、生命周期和持久化；插件负责声明自己的命令、处理器、配置和工具。
新增插件不需要修改 app.py、API 路由或宿主配置模型。插件之间通过已公开能力协作，不互相导入文件。

## 依赖边界

协议、存储、基础配置、事件与规则层不反向依赖应用装配或插件实现。
plugin SDK 可使用 rules 与 onebot；通过 Context 和类型约定访问装配后的宿主，不运行时导入 app.py。
llm 使用配置、协议模型和策略解析，不导入插件包、管理 API 或页面。
app、dispatcher 与 api 负责连接这些能力，用户插件不导入 app 或 api 的内部实现。

`scripts/check_architecture.py` 静态检查这些运行时导入方向，跳过仅供类型标注的 TYPE_CHECKING 引用；
新的跨层依赖应先明确用途并更新架构说明，而不是随意放开边界。检查覆盖显式 Python import，不替代代码审查。

## 新功能放置规则

- 命令、事件和 AI 工具：新建独立插件包，复杂模块仍留在包内。
- 配置字段：插件内 settings.py / plugin.setting；确属宿主通用能力才扩展 config.py。
- 协议消息或 QQ 动作：onebot 与 Context，补协议模拟测试。
- 数据表与持久化：storage，使用迁移而非导入时改表。
- 外部模型或服务：services 独立部署，插件只封装客户端。
- 管理界面：ui 对应功能模块与 api，不能复制机器人执行逻辑。

## 统一验证

```powershell
.venv/Scripts/python.exe scripts/check_architecture.py
.venv/Scripts/python.exe -m fllbot plugin check --builtin
.\scripts\verify.ps1
```

统一脚本校验核心架构、标准插件包、`fllbot/plugins/tests/scripts` 的代码和项目回归测试。
services 有独立运行环境和依赖，应单独验证；不随机器人框架测试启动真实 TTS、模型或 QQ 连接。
插件开发命令和文件头约定见 [插件包规范](plugin-packages.md)，具体 SDK 见 [插件 API](plugin-api.md)。
