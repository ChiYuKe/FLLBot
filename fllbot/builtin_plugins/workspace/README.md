# workspace

沙盒工作区工具包，元信息见 [plugin.toml](plugin.toml)。

## 用途

给智能体一个**受限的动手环境**：只能读写管理员指定的工作区目录，联网只能访问策略允许的主机。
它不执行本机程序，也不接受绝对路径；所有操作在执行前经过宿主沙盒裁决（能力、风险、路径、链接、权限、本轮额度）。

## 工具

| 工具 | 能力 | 说明 |
| --- | --- | --- |
| `workspace__list_files` | `fs.read` | 列出工作区内相对目录 |
| `workspace__read_file` | `fs.read` | 读取工作区内 UTF-8 文本 |
| `workspace__write_file` | `fs.write` | 写文本（默认关闭；自动参与时不执行） |
| `workspace__fetch_url` | `net.read` | 抓取公开 http(s) 文本页面/JSON |
| `workspace__stat` | `fs.read` | 查看文件/目录类型、大小与修改时间 |
| `workspace__search` | `fs.read` | 按关键词或文件名模式搜索（有界） |
| `workspace__make_dir` | `fs.write` | 创建目录 |
| `workspace__move` | `fs.write` | 移动或重命名 |
| `workspace__copy` | `fs.read` + `fs.write` | 复制文件 |
| `workspace__delete` | `fs.write` | 删除文件；目录需要 `recursive=true` |
| `workspace__download` | `net.read` + `fs.write` | 从允许的 http(s) 地址下载文件到工作区 |
| `workspace__send_file` | `fs.read` + `qq.send_file` | 把工作区文件作为附件发送到当前会话 |

文件路径一律相对工作区；`../`、绝对路径、盘符、软链接逃逸和敏感文件名（`.env`、`config.toml`、
`data/admin_token.txt`、数据库、日志等）会被拒绝。每次读取/写入/抓取都有字节上限。

## 启用

1. 在 `config.toml` 的 `[sandbox]` 中确认 `enabled = true`，按需设置 `workspace`、`allow_write`、
   `allow_network`、`allowed_hosts` 等；默认 `allow_write = false`。
2. 在「智能体 → 允许调用的工具」勾选需要的工具；模型只会看到已授权工具。
3. 群级可以收紧或放宽（`sandbox_write`、`sandbox_network`），管理端修改后立即生效。

## 权限与边界

- 自动参与（模型自行判断是否接话）不会执行 `fs.write` 等写入类操作，必须由群友明确请求。
- 破坏性能力和本机程序执行默认关闭，且需要超级管理员。
- 工作区默认是 `data/agent_workspace/`。`workspace_scope = "scene"`（默认）会按群/私聊再分子目录，
  不同群看不到彼此的文件；设为 `shared` 才共用一个目录。
- `send_file` 拒绝 `.exe`、`.dll`、`.msi`、`.bat`、`.cmd`、`.jar`、`.apk` 等可执行/启动器文件，
  单文件受 `max_file_bytes` 限制（默认 5 MB）；自动参与时不会发送。
- 沙盒是进程内策略与资源围栏，不是操作系统级隔离；插件仍以宿主进程权限运行，只安装可信插件。

## 数据位置

工作区文件在 `[sandbox].workspace` 指定的目录，默认按 `group_<群号>` / `private_<QQ>` 隔离；
插件自身不保存额外数据。
