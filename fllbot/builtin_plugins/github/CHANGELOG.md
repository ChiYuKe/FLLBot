# 变更记录

## 0.2.0

- 新增 `list_directory`：按目录逐层浏览并对大目录分页，替代只能依赖 400 条全量文件树的旧方式。
- 新增 `search_repositories`：按关键词与 GitHub 限定符检索公开仓库。
- 新增 `read_releases`、`read_commits`：确认版本、更新说明、最近维护状态与单文件改动时间。
- `read_repository` 增加 `ref`、`tree_path`、`offset`/`limit`，返回语言/主题/许可证/推送时间等元信息。
- `read_repository` 新增 `view=tree` 缩进结构树：`depth` 控制层数（1–4），`tree_path` 展开子树；
  超过 200 行时自动退回完整顶层结构，结构树含图片、`paths` 只列可读文本。
- `read_sources` 支持 `ref` 与 `path#L10-L40` 行范围，返回 `total_lines`/`next_line` 继续读取。
- 新增只读结果缓存（默认 180 秒）与令牌/速率/次要限流分类错误提示，降低 API 额度消耗。
- 文件清单过滤生成目录并按源码/文档优先排序；输出按预算裁剪，避免触发宿主的 JSON 截断。
- 新增 `timeout`、`cache_seconds` 配置项；README 与开发文档同步更新。

## 0.1.0

- 新增只读 GitHub 仓库读取工具：仓库元信息、README、文件清单、图片清单及指定文本文件。
- 新增按批次读取源码工具，支持针对目录撰写有源码依据的详细技术分析。
