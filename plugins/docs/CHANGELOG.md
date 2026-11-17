# 变更记录

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
