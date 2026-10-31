# 用户插件目录

此目录的每个直接子文件夹就是一个完整 FLLBot 插件包。当前 `voice/` 与 `web/` 分别封装语音、联网能力，
代码、包头、使用和开发文档归属于各自包内，不再平铺 `voice.py` / `voice.md`。

新建插件：

```powershell
.venv/Scripts/python.exe -m fllbot plugin new demo --description "演示插件" --author "你的名字"
.venv/Scripts/python.exe -m fllbot plugin check --name demo
.venv/Scripts/python.exe -m fllbot plugin list
```

标准结构：

```text
demo/
  plugin.toml          包头：名称、版本、作者、描述、分类与 API 版本
  __init__.py          只导出 plugin
  plugin.py            插件装配与生命周期
  handlers.py          命令、事件与 AI 工具
  settings.py          声明式配置
  README.md            用户说明
  CHANGELOG.md         更新记录
  docs/DEVELOPMENT.md   开发维护约定
```

注意：

- 文件夹名必须等于包头 name，包内用 `Plugin.from_manifest(__file__)`，元信息不重复维护
- `--template tool` 可生成带显式 AI 工具的模板；新增模块沿用带插件标识与职责的文件头
- 包内使用相对导入，运行文件写 `ctx.data_dir`，配置交给宿主数据库
- 启动、热重载自动发现用户包；修改前先启用新加载器，当前运行的 FLLBot 需要重启一次
- 文件名/目录名以 `_` 或 `.` 开头的会被跳过
- 用 `bot.plugins.failed` 或 `GET /api/plugins` 查看加载失败原因，日志前缀 `fllbot.plugin.loader`

旧单文件与无清单的包暂时兼容，后续开发统一使用标准包。
规范见 [插件包开发](../docs/plugin-packages.md)，接口见 [插件 API](../docs/plugin-api.md)，项目职责见 [架构说明](../docs/architecture.md)。
