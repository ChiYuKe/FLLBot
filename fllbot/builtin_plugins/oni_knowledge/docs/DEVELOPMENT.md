# 开发说明

`service.py` 只负责文件读取与检索；`plugin.py` 使用 SDK 注册工具、设置和 Agent 策略。无需修改核心规则或跨插件导入图片功能。工具输出由 Agent 传给已授权的图片工具。

索引契约：types.csv 至少包含 type、file、line、base；members.csv 至少包含 member、type、file、line。CSV 为 UTF-8，file 为相对源码根目录的路径，line 从 1 开始。

类型表按路径、修改时间和大小缓存；成员表流式检索。通过分页控制模型上下文，读取不拆分单行。编码后的结果预算预留 JSON 开销，超长源码单行明确报错，索引超长字段标记 truncated_fields。更新索引后下次请求重新加载类型表。

只接受管理员配置的索引根目录。解析符号链接后的路径仍须位于根目录中；源码路径必须来自类型表并以 .cs 结尾。索引目录仅允许读取 INDEX.md、AGENTS.md 和 types/*.md。取消或失效会话不会返回读取结果。

验证：`tests/test_oni_knowledge.py` 覆盖检索、分页、授权策略、会话失效和文件边界。不得执行外部知识库脚本。
