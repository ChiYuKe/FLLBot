# 变更记录

## 0.3.0

- `send_file` 在附件确认送达后返回 `silent: true` 和简短中文 `reply`，供宿主直出确认、省掉一次模型往返；
  失败或结果不确定时保持原 `send_failed` 错误，不附加 `silent` / `reply`。
- `download` 等只保存到工作区、未发送给用户的工具不做直出确认。

## 0.2.0

- 新增 `stat`、`search`、`make_dir`、`move`、`copy`、`delete`，补全工作区文件管理。
- 新增 `download`：从允许的公开地址下载文本或二进制到工作区。
- 新增 `send_file`：把工作区文件作为附件发送，拒绝可执行文件，自动参与时被沙盒拒绝。
- 工作区支持按群/私聊隔离（`workspace_scope = "scene"`），避免跨群读取。

## 0.1.0

- 新增沙盒工作区文件列举、读取、写入与受控网页读取工具。
- 工具声明 `fs.read` / `fs.write` / `net.read` 能力，由宿主沙盒统一裁决。
