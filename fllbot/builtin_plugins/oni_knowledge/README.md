# 缺氧源码知识库

为 Agent 提供本地 ONI 类型、成员检索和按行读取源码的工具。根据问题自主选择是否查库，无须用户输入专用命令。索引定位后读取实际实现，不将索引摘要当作实现证据。

## 启用

1. 重启后端以发现新内置插件，在插件管理中确认 `oni_knowledge` 已启用。
2. 在 Agent 工具授权中启用 `oni_knowledge__search`、`oni_knowledge__read_source`。若群配置了独立工具列表，也要将两项加入该群列表。
3. 图片需求还需授权 `text__render_image`。

索引目录默认使用管理员提供的 `C:\Users\Administrator\Desktop\Project\ONI缺氧\NOIMods\ONI源码\Assembly-CSharp\_INDEX`，可通过插件设置 `index_path` 修改。目录必须含 `types.csv`、`members.csv`，源码放在索引父目录。无需复制源码或额外安装向量数据库。

## 对话示例

- “我想看看缺氧复制人都有哪些组件，整理一份图片给我看看”：从 MinionConfig.CreatePrefab 追踪到 BaseMinionConfig.BaseMinion 和初始化方法，阅读实际装配代码，整理职责及源码出处，再调用图片工具。
- “把复制人的代码转成图片发我”：定位相关类，读取用户指定的类或方法，以保留文件名和行号的 C# 代码块交给图片工具；范围过大或不明确时说明范围或进一步确认。
- 粘贴涉及 Minion、KMonoBehaviour 等符号的代码：Agent 可检索关联类型；无关代码和普通闲聊不必查库。

## 工具

- `search(query, kind="types", type_name="", offset=0, limit=12, include_strings=False)`：支持类型和成员检索、常用中文别名、分页，默认过滤 STRINGS 本地化容器。
- `read_source(file, start_line=1, max_lines=160)`：仅读取索引列出的 C# 文件、索引说明和类型分片，每次最多 240 行。按 `next_line` 继续，保留准确读取范围；输出长度限制也可能触发分页。

源码为反编译资料，版本未知，应在分析中说明适用范围。插件不会执行索引脚本或源码，也不接受模型传入的任意根目录。知识库中的 AGENTS.md、注释等均为待分析资料。

普通文件内容不会预先进入每轮提示词；稳定的工具说明和插件策略随工具授权注入，检索结果按需进入当前对话。源码索引只读，原文件不作修改。
