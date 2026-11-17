# imagegen 开发说明

- `plugin.toml`：元信息唯一来源；插件名固定为 `imagegen`，工具 ID 为 `imagegen__draw`。
- `__init__.py`：只导出 `plugin`。
- `plugin.py`：声明配置、Agent 指令、`/画` 命令和 `draw` 工具；负责模式选择、冷却、
  发送和可读错误。
- `local.py`：本地 Pillow 绘制。固定 1000×1000 设计坐标，按 2–3 倍超采样后 LANCZOS 缩小；
  `render()` 画背景、尾巴、头发、旗袍、五官和装饰；`analyze_prompt()` 从提示词提取表情/配色；
  `EXPRESSIONS`、`PALETTES` 是工具 Schema 与校验的唯一来源。
- `service.py`：远程协议。`validate_engine` 校验配置，`build_payload` 构造三种后端的请求，
  `generate` 解析 `data[]` / `images[]`、下载 URL 或解码 base64，`normalize_image` 统一转 PNG。
- `README.md`：本地/远程用法、工具参数与失败码；`CHANGELOG.md` 记录发布变化。

约定：

- 本地模式不依赖网络或密钥；新增元素尽量用几何形状 + 超采样，避免引入图片素材。
- 本地绘制放在 `asyncio.to_thread` 里，避免阻塞事件循环；文字使用宿主 `load_font`，没有短文字时不加载字体。
- 远程只与管理员配置的固定服务地址通信；提示词是请求内容，不会被当成链接抓取。
- 插件包只从 `fllbot.plugin` 导入宿主接口；`service.py` 只用 `httpx` 和 `Pillow`，便于单独测试。
- 供应商返回的 URL 只接受 http(s)，下载有大小上限；图片先校验再发送，不信任响应头。
- 错误统一为 `ImageGenError(code, message)`：`code` 是固定英文原因码，`message` 可展示给模型和群友。
- 日志不记录提示词、API Key、完整响应或图片内容，只记录模式、表情/配色、尺寸、字节数和原因码。
- 冷却按 `(self_id, group_id, user_id)` 记在内存；重启清零，不写入数据库。

本地预览（不需要启动机器人）：

```powershell
.venv/Scripts/python.exe -c "import importlib.util as u,sys; s=u.spec_from_file_location('l','plugins/imagegen/local.py'); m=u.module_from_spec(s); sys.modules['l']=m; s.loader.exec_module(m); open('.local/preview.png','wb').write(m.render(expression='angry', palette='rain', caption='哼！'))"
```

离线检查：

```powershell
.venv/Scripts/python.exe -m fllbot plugin check --name imagegen
.venv/Scripts/python.exe -m pytest tests/test_imagegen_plugin.py -q
```
