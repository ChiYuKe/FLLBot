# memory 开发与维护

`memory` 是外置插件包：命令、AI 工具与周期清理共用一个 SQLite 文件，宿主只负责插件开关、群覆盖、权限与工具沙盒裁决。

## 模块划分

| 文件 | 职责 |
| --- | --- |
| `plugin.py` | 包头实例、配置字段、提示词片段、AI 工具、命令与周期清理装配 |
| `store.py` | SQLite 建表/迁移、隔离键 `Scope`、按范围查询与删除 |
| `README.md` | 使用说明与默认值 |
| `CHANGELOG.md` | 版本变化 |

插件只从 `fllbot.plugin` 导入宿主接口（`Context`、`Plugin`），运行期通过 `ctx.settings`、`ctx.data_dir` 协作；不导入 `fllbot/` 内部模块，也不碰宿主数据库。

## 隔离模型

`store.Scope` 是唯一的访问边界：

```text
(self_id, scene, scene_id, user_id)
```

- `scene=group` 时 `scene_id` 是群号；`scene=private` 时 `scene_id` 是用户号；
- `user_id` 永远是当前发言人，从 `ctx.user_id` 读取，**没有任何工具参数可以覆盖它**；
- 所有读、写、删都带完整四元组，跨群、群与私聊、不同用户、不同机器人账号互不可见；
- 私聊里写入的记录不会出现在任何群，群记录也不会出现在私聊。

`store.scope_of(ctx)` 缺少账号、用户或会话时抛出 `ValueError`，工具层转成 `invalid_scope` 返回模型。

## 表结构与迁移

`data/plugins/memory/memory.sqlite3`（WAL、`busy_timeout=5000`）：

```sql
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    self_id INTEGER NOT NULL,
    scene TEXT NOT NULL CHECK(scene IN ('group','private')),
    scene_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_key TEXT NOT NULL,          -- casefold 后的内容，用于去重
    kind TEXT NOT NULL DEFAULT 'fact',
    tags TEXT NOT NULL DEFAULT '',      -- 逗号连接的规范化标签
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX facts_scope ON facts(self_id, scene, scene_id, user_id, id DESC);
CREATE INDEX facts_expiry ON facts(expires_at);
```

迁移用 `PRAGMA user_version`：当前 `SCHEMA_VERSION=1`，首次打开建表；后续版本在 `store.migrate()` 里追加分支，不执行宿主迁移。每次操作打开/关闭连接，热重载不会残留连接。

## 写入策略

`memory__remember` 的顺序：

1. 配置 `enabled`，再检查 `trigger_decision.addressed`（见下）；
2. 规范化内容：折叠空白、校验长度、命中敏感词直接拒绝；
3. 校验 `kind` 与标签（最多 8 个、每个 20 字符，逗号持久化）；
4. 清理过期记录；
5. 同范围同内容去重：命中则刷新 `created_at`/`expires_at` 并返回已有编号；
6. 统计有效条数，达到 `max_facts` 返回 `limit_reached`，不静默淘汰；否则插入。

`expires_at = created_at + retention_days * 86400`。过期清理有三个入口：写入前、`recall`/命令读取时按 `expires_at > now` 过滤、`prune_expired` 周期任务（每小时，首次延迟 120 秒）全局删除。

## effect / risk 与自动参与边界

- `memory__recall`：`effect="read_only"`、`capabilities=("memory.read",)`，风险推导为 `read`；只读工具可参与宿主同轮结果复用。
- `memory__remember` / `memory__forget`：`effect="side_effecting"`、`risk="write"`，不声明沙盒能力。

宿主 `AIToolSpec` 的 effect 枚举只有 `read_only` / `idempotent` / `side_effecting`，没有 `write`；写语义对应 `side_effecting`。宿主沙盒能力表只有 `memory.read`，没有 `memory.write`，而 `guard.decide()` 的 `automatic_side_effect` 只检查 `fs.write`/`net.write`/`email.send`/`qq.send_file`/`qq.manage`/`screen.capture`/`process.exec`。因此**写工具不会命中宿主级自动参与拒绝**。

为避免把记忆读写伪装成文件/网络能力（会错误要求 `allow_write` 等沙盒开关），本插件沿用 poke/emotion 的既有先例：在 `remember`/`forget` 回调里读取 `ctx.trigger_decision.addressed`，自动参与（未 @、未唤醒、非私聊明确请求）时返回：

```json
{"error": "addressed_required", "message": "只有当前用户明确要求记住或忘记时才能写入记忆；自动参与时不会保存或删除。"}
```

回调先于任何数据库写入，因此拒绝不会产生记录。`recall` 不拦截：它只读取当前用户自己的记录，自动参与时可用于自然引用记忆。若将来宿主新增 `memory.write` 能力，应改为声明该能力并删除回调内的 addressed 检查，让沙盒统一裁决。

## 提示词片段

`plugin.agent_instruction()` 声明三个片段，分别关联 `remember`、`recall`、`forget`：

- `memory_remember`：何时保存、主体只能是发言人、禁止敏感信息与第三方隐私；
- `memory_recall`：何时回查、只使用返回结果、不要编造；
- `memory_forget`：何时删除、单条用编号、清空先确认。

宿主 `plugin.agent.instruction_sections()` 仅在片段关联的工具**全部**进入本轮权限白名单时注入，所以未授权的工具不会带来提示词。`auto_hint=False` 时 `MemoryPlugin.agent_instructions` 返回空元组，不再注入（`auto_hint` 是全局配置，不做群级覆盖，因为片段在请求级组装）。

## 资源清理

插件不持有长期连接或后台任务：SQLite 每次操作后关闭，过期清理用 `@plugin.on_loop(3600, initial_delay=120)`，卸载时由宿主取消。热重载会重建模块级 `plugin`，数据文件保留。

## 验证

```powershell
.venv/Scripts/python.exe -m pytest tests/test_memory_plugin.py -q
.venv/Scripts/python.exe -m ruff check plugins/memory tests/test_memory_plugin.py
.venv/Scripts/python.exe -m fllbot plugin check --root . --name memory
.venv/Scripts/python.exe scripts/check_architecture.py
```

离线测试使用 `tests/helpers.py` 的 `FakeConnection` / `group_message` / `make_bot`；不访问真实 QQ 或网络。新增行为时优先补：隔离键、条数/长度上限、去重、过期清理、命令、自动参与拒绝、插件开关与提示词可见性。
