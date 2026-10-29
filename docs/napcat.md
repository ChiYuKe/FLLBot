# 接入 NapCat

## 连接方向

FLLBot 是 **反向 WebSocket 的服务端**：NapCat 启动后主动连过来。

```
NapCat（QQ 协议端）  ──ws──▶  FLLBot（后端）
     ▲                          │
     └────── API 调用 ───────────┘   （send_msg、get_group_list、set_group_ban…）
```

不要配置 NapCat 的「正向 WebSocket」或「HTTP 上报」，FLLBot 不监听客户端连接，
也不接收 HTTP POST 事件。

## NapCat 侧配置

打开 NapCat WebUI →「网络配置」→ 新增 → 选择 **WebSocket 客户端**（即反向 WebSocket）：

| 配置项 | 建议值 |
| --- | --- |
| 名称 | FLLBot |
| URL | `ws://127.0.0.1:6701/onebot/v11/ws` |
| Token | 与 `config.toml` 中 `server.access_token` 完全一致；两边都留空则不校验 |
| 消息格式 | `array`（推荐；FLLBot 也兼容 CQ 字符串） |
| 启用 | ✅ |
| 心跳间隔 | 默认 5000ms 即可（FLLBot 默认 120s 没收到任何数据才断开） |
| 重连间隔 | 默认即可，NapCat 断线会自动重连 |

保存后 FLLBot 日志应出现：

```
INFO fllbot.onebot: NapCat 已连接：self_id=10001 role=Universal 当前连接数=1
```

同时 `GET /api/connections` 能看到该连接。

## FLLBot 侧配置

```toml
[server]
host = "127.0.0.1"
port = 6701
ws_path = "/onebot/v11/ws"
access_token = ""       # 建议设置；留空则任何本机进程都能连上
heartbeat_timeout = 120.0
```

> 路径其实无所谓：FLLBot 接受任意路径的 WS 连接（`/onebot/v11/ws`、`/ws`、`/`、`/custom` 都可以），
> 这样 NapCat 里填错路径也能连上。

非本机部署（比如 NapCat 跑在另一台机器）时：

- `host` 改成 `0.0.0.0`
- **必须**设置 `access_token`
- 防火墙放行端口
- 最好再加一层反代/VPN；OneBot 端口没有传输加密

## 多账号

一个 QQ 号对应一条反向连接。多个 NapCat 实例（或多个账号）同时连到同一个 FLLBot 即可：
FLLBot 按事件里的 `self_id` 区分，并在回复时路由到对应连接。

- `GET /api/connections` 查看全部
- 管理 API 透传调用时用 `self_id` 指定走哪条连接
- 插件里 `ctx.connection` 始终是触发事件的那条连接，多账号天然正确

## 常见问题

| 现象 | 排查 |
| --- | --- |
| NapCat 显示连接失败 / 403 | FLLBot 没启动；端口不对；`server.host` 只监听了 `127.0.0.1` 而 NapCat 在别的机器 |
| 连上就断开，FLLBot 日志 `WS 鉴权失败` | 两边 Token 不一致（注意首尾空格）；或 NapCat 把 Token 当成了 query 参数 |
| 连上但发消息没反应 | 群里是否被停用（`GET /api/groups`）；插件是否启用（`GET /api/plugins`）；看 `GET /api/logs?level=WARNING` |
| 收到消息但机器人不回 | `/ping` 也不回 → 连接断了吗？`/api/status` 的 `connections` 是否 online |
| 回复发送失败，`retcode` 非 0 | 被风控 / 群禁言 / 消息内容被拦截；看日志里的 `ActionError` |
| 心跳超时被断开 | `heartbeat_timeout` 调大；检查 NapCat 是否在疯狂重启 |
| 撤回/禁言等接口报错 | NapCat 版本差异，先用 `POST /api/actions` 手工试 |

## 调试顺序（推荐）

1. `python -m fllbot check` —— 配置是否合法
2. `python -m fllbot run --root .` —— 看启动日志（端口、插件数、令牌）
3. `python tools\smoke_test.py` —— 不起 QQ 也能验证 WS + API 链路
4. 用 `tools\fake_napcat.py` 模拟消息，确认插件逻辑
5. 最后再在真实 NapCat 上发 `/ping`

## 与 NoneBot 混用的注意点

同一个 NapCat 可以同时连多个框架（多条反向 WS），但**同一个账号不要同时被两个框架
以相同 `self_id` 处理同一批消息**，否则会出现重复回复、上下文互相污染。
FLLBot 的群开关、插件开关只作用于自己，不感知其他框架。
