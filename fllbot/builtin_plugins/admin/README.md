# admin

运维命令：查看运行状态、热重载插件、远程开关插件与群。

## 命令

| 命令 | 权限 | 说明 |
| --- | --- | --- |
| `/status` | superuser | 查看版本、运行时长、连接数、插件数、消息统计 |
| `/运行状态` `/状态` | superuser | 别名 |
| `/reload <插件名>` | superuser | 热重载指定插件 |
| `/重载` | superuser | 别名 |
| `/plugin on\|off <插件名>` | superuser | 启用/停用插件 |
| `/插件开关` | superuser | 别名 |
| `/group on\|off` | 群管理员 | 启用/停用本群的机器人 |

## 示例

```text
> /status
FLLBot 0.2.0
运行时长：2 小时 13 分
连接：1 个（10001）
插件：9 个，已启用 9 个
消息：收到 128 条，发送 96 条

> /plugin off reminder
插件 reminder 已停用
```

## 说明

- `superuser` 在 `config.toml` 的 `bot.superusers` 里配置（QQ 号列表）
- `/reload` 会重新读取插件源码，适合改完插件文件后不重启机器人
- `/group off` 只影响当前群，状态写入 SQLite，重启后仍生效
