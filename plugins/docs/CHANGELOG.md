# 变更记录

## 未发布

- `docs__write_docx` / `write_xlsx` / `write_pptx` 在附件确认送达后返回 `silent: true` 与简短
  `reply`（“文档已经发给你啦”），供宿主直出确认、省掉一次模型往返；生成成功但发送失败、回执缺失或
  参数/生成/沙箱失败时不设置，原有成功字段不变。

## 0.1.1

- 新增周期清理 `cleanup_files`（每 6 小时，`initial_delay` 5 分钟）并在 `on_load` 时清理一次；
  只删除 `files/` 下本插件生成的文件（`<12 位十六进制>_<附件名>`），同目录其它文件保持不动，
  清理失败只记日志，不影响插件加载与发送。
- 三个写工具增加 `addressed` 兜底：自动参与（未 @、未叫到）返回 `docs_addressed_required`，
  不生成文件也不发送；即使管理员把 `[sandbox].enabled` 设为 false、宿主不做能力裁决也成立。
- `max_file_mb` 默认从 8 降到 5，与工作区 `send_file` 默认附件上限一致，避免 base64 大文件进入
  消息历史；README 说明可按 NapCat/QQ 附件能力调整。
- 复核失败清理：生成/校验/发送失败只删除本次生成文件，磁盘名保留随机前缀，不误删并发请求文件。

## 0.1.0

- 新增 `docs` 文档生成插件：`docs__write_docx`、`docs__write_xlsx`、`docs__write_pptx` 三个工具，
  生成 Word / Excel / PPT 并通过 QQ 文件附件发送。
- 三个工具均声明 `qq.send_file` 能力（`side_effecting` / `risk=write`）：自动参与被沙盒拒绝，
  需要「智能体 → 允许调用的工具」授权后模型才可见。
- 受控内容结构：Word 支持 heading/paragraph/bullets/table，Excel 支持字符串/数字/布尔单元格，
  PPT 支持标题页 + 内容页要点；不接受图片、超链接、公式、任意 XML 或模板。
- 文件名严格清洗并强制扩展名；生成文件先落盘再用对应库重新打开校验，失败不发送。
- 生成到 `data/plugins/docs/files/`，按 `retention_days` 清理旧文件；发送失败删除临时文件。
- 配置新增启用开关、单文件大小、正文总字数、章节/页/行列/单元格/要点上限、保留天数与中文字体。
- 依赖新增 python-docx、openpyxl、python-pptx（含传递依赖 lxml、et-xmlfile、XlsxWriter）。
