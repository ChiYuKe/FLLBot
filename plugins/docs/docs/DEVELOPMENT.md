# docs 开发说明

- `plugin.toml`：元信息唯一来源；插件名固定为 `docs`，工具 ID 为 `docs__write_docx`、
  `docs__write_xlsx`、`docs__write_pptx`。`dependencies` 声明 python-docx、openpyxl、python-pptx。
- `__init__.py`：只导出 `plugin`。
- `plugin.py`：声明配置、Agent 指令与三个 `on_ai_tool`；负责把配置转换成 `service.Limits`、
  清洗文件名、检查 `addressed` 兜底、调用生成器、通过插件 API 发送附件、返回可读结果。
  同时提供 `DocsPlugin.on_load` 清理与 `cleanup_files` 周期任务（每 6 小时）。
- `service.py`：纯本地实现，不导入宿主模块。包含受控结构校验（`normalize_*`）、三种格式的
  生成器（`build_*`）、重新打开校验（`validate_*`）、落盘（`_commit`）与保留期清理（`cleanup`）。
- `README.md`：用户向的参数、配置与失败码；`CHANGELOG.md` 记录发布变化。

## 数据流

```text
工具回调 → _addressed 兜底（自动参与返回 docs_addressed_required，不生成文件）
        → 读 ctx.settings → service.sanitize_filename
        → asyncio.to_thread(service.write_*)
              normalize_*：结构/类型/字数/行列上限，拒绝未知字段与控制字符
              build_*：python-docx / openpyxl / python-pptx 公开 API 写内存
              _commit：大小上限 → 写 data_dir/files/<12位uuid前缀>_<文件名> → validate_* 重新打开校验
        → ctx.data_current 复核 → send_media_reply(MessageSegment.file(base64://...))
        → 成功：cleanup(retention_days, keep=本次文件)；失败：删除本次文件并返回可读错误
```

清理有三个入口，共用 `service.cleanup`：

| 入口 | 时机 | 说明 |
| --- | --- | --- |
| `DocsPlugin.on_load` | 插件加载/热重载 | 立即清一次，避免长期闲置后第一次使用才清理 |
| `@plugin.on_loop` `cleanup_files` | 每 6 小时，`initial_delay` 5 分钟 | 周期清理；回调通过 `bot.plugins.get("docs")` 找已加载实例 |
| `_send_document` 发送成功后 | 每次成功发送 | 传 `keep=本次文件`，顺带清理旧文件 |

清理只处理文件名匹配 `^[0-9a-f]{12}_` 的本插件生成文件，同目录其它文件不动；
`DocsPlugin.on_load` 和周期任务失败只记日志，不影响插件加载与发送。

要点：

- **只写插件数据目录**：生成文件放在 `ctx.data_dir / "files"`，不写沙盒工作区、仓库或系统临时目录。
  发送走宿主插件 API `send_media_reply` + `MessageSegment.file("base64://...", name=...)`；
  沙盒的 `ctx.sandbox.files.send_file` 只用于工作区文件，这里不使用。
- **自动参与双保险**：工具声明 `qq.send_file`，宿主沙盒在自动参与时拒绝；回调里再用 `_addressed`
  检查 `ctx.trigger_decision`。判断规则与宿主一致：无触发决策视为明确请求，`addressed` 显式值优先，
  否则 `trigger_kind` 为 `at`/`prefix` 才算明确请求；`entrypoint != "qq"` 视为明确请求。
  这样即使 `[sandbox].enabled=false`，自动参与也不会生成文件。
- **先校验再发送**：`validate_*` 用对应库重新打开刚写入的文件并核对标题/工作表/页数，
  校验失败删除文件并返回 `docs_generate_failed`，不会把损坏文件发出去。
- **不发送半成品**：生成抛错时不产生文件；校验失败、发送异常或返回 false 时只删除本次
  `<12位uuid前缀>_<文件名>`，不碰并发中的其它请求文件。
- **文件名唯一化**：磁盘名带 12 位随机前缀避免并发覆盖；QQ 附件名使用清洗后的用户文件名。
- **中文字体**：Word 通过 `w:eastAsia`、PPT 通过 DrawingML `a:ea` 显式设置中文字体，
  Excel 用 `Font(name=...)` 写入；字体名来自配置并用 `safe_font_name` 过滤不可打印字符。
- **公式注入**：openpyxl 会把以 `=` 开头的字符串保存为公式，`_set_xlsx_cell` 强制把这类文本
  改回字符串类型（`cell.data_type = "s"`），Word/PPT 只写文本节点，不创建超链接或 XML 片段。
- **失败可读**：所有业务错误统一抛 `DocsError(code, message)`；`plugin.py` 不向模型透出异常栈或本机路径。

## 校验与限制

| 限制 | 来源 | 校验位置 |
| --- | --- | --- |
| 单文件大小 | `max_file_mb`（默认 5.0，与工作区 send_file 默认 5 MB 对齐）→ `Limits.max_file_bytes` | `_commit`（写盘前） |
| 正文总字数 | `max_chars` | `_text` / `_table_cell`（累计） |
| Word 章节数 | `max_blocks` | `normalize_docx` |
| PPT 内容页 | `max_slides` | `normalize_pptx` |
| 表格行列/单元格 | `max_rows`、`max_cols`、`max_cells`、`max_cell_chars` | `normalize_xlsx`、`_table` |
| 要点条数 | `max_bullets` | `normalize_docx`、`normalize_pptx` |
| 文件名 | 固定规则 | `sanitize_filename` |

工具 JSON Schema 里的 `maxItems` / `maxLength` 只是模型侧的第一道约束，
真正的上限仍以配置和 `service.py` 校验为准（配置可以调低）。

## 测试

`tests/test_docs_plugin.py` 离线运行：把 `plugins/docs` 复制到临时插件目录后由宿主加载，
monkeypatch `bot.deliveries.send_media` 截获附件，再用 python-docx / openpyxl / python-pptx
重新打开生成文件核对内容。覆盖：

- 三种格式的正文、表格、表头加粗/冻结首行/列宽、封面与要点，磁盘文件与发送字节都能重新打开；
- 文件名强制扩展名、路径穿越/隐藏名/非法字符/保留名拒绝；
- 章节结构、单元格类型、行列/页/字数/大小上限，以及默认 5 MB 上限口径；
- 自动参与被沙盒拒绝；`[sandbox].enabled=false` 时由 `_addressed` 兜底拒绝且不生成文件；
- 未授权时工具不可见、发送失败与依赖缺失的失败路径；
- `on_load` 与周期 `cleanup_files` 只清理过期生成文件、保留非本插件文件；
- 生成/校验/发送失败只删除本次文件，不误删并发文件；data_dir 落盘位置。


## 验证

```powershell
.venv/Scripts/python.exe -m pytest tests/test_docs_plugin.py -q
.venv/Scripts/python.exe -m ruff check plugins/docs tests/test_docs_plugin.py
.venv/Scripts/python.exe scripts/check_architecture.py
.venv/Scripts/python.exe -m fllbot plugin check --name docs
```

注意：`plugin check` 只解析 TOML、文件结构、Python 语法与静态导入边界，不执行插件代码；
运行时初始化、真实发送文件和 QQ 侧附件能力仍需实际加载验证。修改插件后热重载会重新导入包；
若替换了依赖版本，先重启宿主再验证。
