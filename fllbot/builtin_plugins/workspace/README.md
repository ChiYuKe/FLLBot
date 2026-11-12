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
- 工作区默认是 `data/agent_workspace/`，随项目数据目录一起被 git 忽略。
- 沙盒是进程内策略与资源围栏，不是操作系统级隔离；插件仍以宿主进程权限运行，只安装可信插件。

## 数据位置

工作区文件在 `[sandbox].workspace` 指定的目录；插件自身不保存额外数据。
