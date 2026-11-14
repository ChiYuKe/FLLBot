# poke 开发说明

- `plugin.toml`：元信息唯一来源；插件名固定为 `poke`，工具 ID 为 `poke__poke`。
- `__init__.py`：只导出 `plugin`。
- `plugin.py`：通知处理器、四个群级设置、昵称补查与主动戳人工具。
- `README.md`：使用说明；`CHANGELOG.md` 记录发布变化。

## 关键依赖

- 事件：`notice.notify.poke`，`user_id` 是戳人者，`target_id` 是被戳者，没有 `message_id`。
- 回合：`fllbot.plugin.context.notice_turn_context(ctx, text, nickname=..., user_id=...)`
  把通知包装成普通群消息上下文，再交给 `ctx.llm.respond()`；不要自行拼模型请求或写历史。
- 发送：`ctx.poke(user_id)` 自动选择群戳/好友戳；失败抛 `ActionError`，不要重试写操作。
- 频率：回戳按人、回应按群使用 `ctx.bot.permissions.cooldown_ok()`；工具另有 5 秒同目标冷却。

## 边界

- 只处理群聊；私聊戳、群友互戳、机器人自己发起的戳直接返回。
- 被戳回应不依赖 `agent` 插件；主动戳人工具由 `agent` 在获得授权后调用。
- 需要文件、联网等副作用时使用宿主沙盒；本插件只声明 `qq.send`，不直接访问数据库或宿主内部模块。

## 验证

```powershell
.venv/Scripts/python.exe -m pytest tests/test_poke.py -q
.venv/Scripts/python.exe -m fllbot plugin check --root . --name poke
.venv/Scripts/python.exe scripts/check_architecture.py
```
