# web 开发说明

当前保留原有实现，统一为目录包：

- `plugin.toml`：元信息唯一来源；稳定 name 保留原有配置、权限与工具标识。
- `__init__.py`：只导出 plugin。
- `plugin.py`：命令处理器与智能体工具；搜索、网页读取、天气/时间的编排与 Open-Meteo 数据源。
- `qq_weather.py`：腾讯 QQ 天气数据源。先用 `wis.qq.com/city/like` 解析省/市/区，再取观测、逐小时/逐日预报、
  空气质量、日出日落和生活指数，整理成与 Open-Meteo 版相同的结构；只覆盖国内城市。
- `weather_card.py`：天气卡片排版。只用 Pillow 绘图，不加载网页或外部图片；按 2 倍超采样绘制再缩回 900px，
  布局参考 QQ 天气（深色场景、逐小时、生活指数、逐日），城市剪影与雨丝/星空都用几何图形绘制。
  图形先画在透明层、合入底图后再写文字，避免半透明面板压住文字；渲染失败返回空字节，由 `plugin.py` 退回文字天气。
- `README.md`：使用说明、命令与依赖；`CHANGELOG.md` 记录发布变化。

包内使用相对导入；模块文件头注明插件标识和职责。导入时不启动服务。
新增资源通过 plugin.on_cleanup() 登记清理；周期任务使用 on_loop，交给宿主管理。
运行文件写入 plugin.data_dir 或 ctx.data_dir，配置使用已有 plugin.setting()。

完整接口见宿主 docs/plugin-api.md；离线检查命令：

```powershell
.venv/Scripts/python.exe -m fllbot plugin check --name web
```
