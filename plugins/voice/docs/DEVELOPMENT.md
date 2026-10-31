# voice 开发说明

当前保留原有实现，统一为目录包：

- `plugin.toml`：元信息唯一来源；稳定 name 保留原有配置、权限与工具标识。
- `__init__.py`：只导出 plugin。
- `plugin.py`：现有处理器、配置与工具实现；后续复杂修改按职责提取包内模块。
- `README.md`：使用说明、命令与依赖；`CHANGELOG.md` 记录发布变化。

包内使用相对导入；模块文件头注明插件标识和职责。导入时不启动服务。
新增资源通过 plugin.on_cleanup() 登记清理；周期任务使用 on_loop，交给宿主管理。
运行文件写入 plugin.data_dir 或 ctx.data_dir，配置使用已有 plugin.setting()。

完整接口见宿主 docs/plugin-api.md；离线检查命令：

```powershell
.venv/Scripts/python.exe -m fllbot plugin check --name voice
```
