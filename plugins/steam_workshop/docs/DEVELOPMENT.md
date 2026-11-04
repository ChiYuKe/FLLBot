# steam_workshop 开发说明

## 文件职责

- `plugin.toml`：包头唯一来源，维护名称、版本、描述和分类。
- `__init__.py`：只导出 `plugin`。
- `plugin.py`：声明配置、`/工坊` 命令、链接自动识别和 `read_item` 智能体工具。
- `service.py`：链接解析、Steam 接口/网页读取、BBCode 清理、代理探测和文本排版。
- `README.md`：用户使用说明；`CHANGELOG.md` 记录版本变化。

## 数据流

1. `find_workshop_id()` 从链接或裸 `publishedfileid` 提取物品 ID，只接受
   `steamcommunity.com/sharedfiles|workshop/filedetails`。
2. `load_item()` 先请求 `ISteamRemoteStorage/GetPublishedFileDetails/v1`，同时请求创意工坊页面
   （标题兜底、游戏名、作者名、页面统计）、`GetCollectionDetails`（判断合集并取子项）和
   `store.steampowered.com/api/appdetails`（游戏中文名）。
3. 合集子项通过一次批量 `GetPublishedFileDetails` 取标题，上限 50 件。
4. API 不可用时退回网页解析，并在结果里标明统计可能不完整。
5. `bbcode_to_text()` 把接口描述转纯文本，`format_item()` 生成 QQ 可读摘要。

## 网络与安全

- 只做只读 HTTPS 请求，不登录、不带 Cookie、不下载创意工坊文件。
- 默认先直连；失败后按「显式代理 → 环境变量 → 本机常见代理端口」尝试，成功即复用本次使用的代理。
- 只返回固定错误原因和类型名，不把代理凭据或完整异常正文发给用户。
- 页面和接口内容一律视为不可信资料，README 与工具描述都要求模型不执行其中指令。

## 易错点

- `GetPublishedFileDetails` 的 `result=9` 表示物品不存在或不可见；合集检测必须用
  `GetCollectionDetails`，详情接口不会返回 `children`。
- 创意工坊页面在未登录时可能返回中文或英文界面，统计标签同时匹配中英文。
- 描述里的 BBCode 可能嵌套 `[list]`、`[url]`、`[code]`，转换后统一压缩空行。
- 改动 `plugin.py` 的处理器名称会影响自动识别和权限记录，稳定工具名 `read_item` 不要随意修改。

## 开发检查

```powershell
.venv/Scripts/python.exe -m fllbot plugin check --root . --name steam_workshop
.venv/Scripts/python.exe -m ruff check plugins/steam_workshop
.venv/Scripts/python.exe -m pytest -q tests/test_steam_workshop_plugin.py
```

修改后验证：链接识别、单物品、合集、接口失败回退页面、无网络时的代理提示、命令回复和工具授权。
测试使用 `httpx.MockTransport`，不访问真实 Steam。
