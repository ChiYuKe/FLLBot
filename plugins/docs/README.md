# docs 文档生成

让群友说一句“做个表格 / 报告 / PPT 发我”，插件生成 **Word（.docx）**、**Excel（.xlsx）** 或
**PowerPoint（.pptx）** 文件，并通过 QQ 文件附件发送到当前会话。

> 包头与元信息见 [plugin.toml](plugin.toml)。工具需要管理员在「智能体 → 允许调用的工具」里授权，
> 且必须由群友明确请求；自动参与时宿主沙盒会拒绝发送文件。

## 用途

| 工具 ID | 生成 | 典型请求 |
| --- | --- | --- |
| `docs__write_docx` | Word 文档 | “把这份季度总结整理成 Word 发我”“写个报告发群里” |
| `docs__write_xlsx` | Excel 表格 | “做个成绩表”“把这些数据整理成表格发我” |
| `docs__write_pptx` | PPT 演示文稿 | “做个项目汇报 PPT”“整理成几页演示文稿” |

三个工具都声明 `qq.send_file` 能力，`effect=side_effecting`、`risk=write`：
文件生成后作为 QQ 文件附件发送；发送失败会删除临时文件并返回 `docs_not_delivered`，
不会留下或谎报半成品。

## 工具参数

### `docs__write_docx(title, sections[], filename?)`

`sections` 按顺序渲染，每项**只设置** `heading` / `paragraph` / `bullets` / `table` 之一：

```json
{
  "title": "季度总结",
  "filename": "季度总结",
  "sections": [
    {"heading": "背景", "level": 1},
    {"paragraph": "本季度完成多项工作。"},
    {"bullets": ["完成 A 项目", "完成 B 项目"]},
    {"table": {"headers": ["项目", "状态"], "rows": [["A", "完成"], ["B", "进行中"]]}}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `heading` | 章节标题文字，可用 `level` 1–3 控制级别 |
| `paragraph` | 一段正文，可含换行 |
| `bullets` | 要点数组，逐条显示为项目符号 |
| `table` | `{headers?, rows}`：`rows` 必填，各行列数必须一致 |

### `docs__write_xlsx(sheet_name, rows[][], header=true, filename?)`

```json
{
  "sheet_name": "成绩",
  "header": true,
  "rows": [["姓名", "分数", "及格"], ["小明", 95, true], ["小红", 88.5, false]]
}
```

单元格只允许字符串、数字或布尔值。`header=true` 时第一行加粗为表头并冻结首行，
列宽按内容简单调整；以 `=` 开头的文本按纯文本写入，不会变成公式。

### `docs__write_pptx(title, slides[{title, bullets[]}], filename?)`

```json
{
  "title": "项目汇报",
  "slides": [
    {"title": "目标", "bullets": ["提升效率", "降低成本"]},
    {"title": "计划", "bullets": ["第一步：调研", "第二步：落地"]}
  ]
}
```

封面页使用 `title`，`slides` 生成内容页；`bullets` 可以为空数组。

### 文件名

`filename` 可省略（按标题/工作表名生成），只接受**文件名本身**：

- 禁止路径分隔符、盘符、`..`、以点开头的隐藏名和系统保留名；
- 只允许中文、字母、数字、空格与 `-_.()（）·`，其余字符直接拒绝；
- 扩展名由工具强制：`.docx` / `.xlsx` / `.pptx`，传 `.txt` 会被替换成对应扩展名。

生成文件位于 `data/plugins/docs/files/`，发送成功后按 `retention_days` 清理旧文件。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | 开 | 群级开关；关闭后本群不生成、不发送 |
| `max_file_mb` | 8.0 | 单文件大小上限（MB），超限直接拒绝 |
| `max_chars` | 50000 | 标题、正文、要点与表格文字总字数上限 |
| `max_blocks` | 60 | Word 章节数上限（表格按一个章节计） |
| `max_slides` | 30 | PPT 内容页上限（不含封面） |
| `max_rows` | 2000 | 表格行数上限 |
| `max_cols` | 50 | 表格列数上限 |
| `max_cells` | 20000 | Excel 单元格总数上限（含表头） |
| `max_cell_chars` | 500 | 单元格字数上限 |
| `max_bullets` | 50 | Word 单组要点 / PPT 单页要点条数上限 |
| `retention_days` | 3 | 生成文件保留天数 |
| `font_name` | 微软雅黑 | 写入 Office 文件的中文字体；非法时回退默认值 |

## 权限与安全

- 工具声明 `qq.send_file`：**自动参与（未 @、未叫到）会被沙盒拒绝**，需要群友明确请求。
- 未在「智能体 → 允许调用的工具」里勾选对应工具时，模型看不到它们。
- 只接受纯文本、数字与布尔值，通过 python-docx / openpyxl / python-pptx 公开 API 写入：
  不支持图片、超链接、公式、任意 XML 或模板，也没有文件路径参数。
- 生成文件只写插件 data_dir，不写工作区或仓库目录；发送失败即删除。
- 日志只记录格式、字节数和固定原因码，不记录正文或文件名以外的用户内容。

## 依赖

宿主环境需要安装（`pyproject.toml` 已声明）：

| 包 | 版本 |
| --- | --- |
| python-docx | `>=1.2,<2` |
| openpyxl | `>=3.1,<4` |
| python-pptx | `>=1.0,<2` |

缺失依赖时插件加载会被清单检查阻止；运行期缺失则工具返回 `docs_dependency_missing`。

## 常见失败

| 返回 | 含义 |
| --- | --- |
| `docs_disabled` | 本群关闭了文档生成 |
| `docs_filename_invalid` | 文件名含路径、`..`、隐藏名前缀或非安全字符 |
| `docs_filename_empty` | 文件名只有扩展名或为空 |
| `docs_content_invalid` | 章节/表格结构非法（多个类型、未知字段、列数不一致等） |
| `docs_limits_exceeded` | 章节、页、行、列、单元格或要点数超过配置上限 |
| `docs_text_too_long` | 正文总字数或单段字数超限 |
| `docs_cell_invalid` | 单元格不是字符串/数字/布尔值，或内容含控制字符 |
| `docs_too_large` | 生成文件超过 `max_file_mb` |
| `docs_dependency_missing` | 缺少 python-docx / openpyxl / python-pptx |
| `docs_generate_failed` | 生成的文件无法通过重新打开校验 |
| `docs_filesystem_error` | 写入 data_dir 失败（磁盘/权限） |
| `docs_context_expired` | 生成期间会话失效，未发送 |
| `docs_not_delivered` | 文件已生成但没有送达 |

## 开发与维护

生成、校验与清理在 [service.py](service.py)，工具声明与发送在 [plugin.py](plugin.py)；
模块划分、校验流程与验证命令见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)，
版本变化见 [CHANGELOG.md](CHANGELOG.md)。
