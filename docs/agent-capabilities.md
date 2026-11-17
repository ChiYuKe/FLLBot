# 群聊 Agent 能力清单与边界

这份清单按“群友可能提出的请求”整理当前可做、需要开启、以及刻意不做的能力。
新增插件工具时先看这里，避免重复实现或越过沙盒契约。

## 聊天与理解

| 需求 | 能力 | 状态 |
| --- | --- | --- |
| 群聊闲聊、接话、判断是否插话 | Agent + 自动参与评分 + `stay_silent` | 默认可用（需开启群响应） |
| @ / 唤醒词 / 引用回复 | 触发策略与引用消息解析 | 默认可用 |
| 被群友戳一戳 | `poke` 插件（复用 `ctx.llm` 回合） | 安装并启用插件；可关回应/回戳并设冷却 |
| 主动戳群友 | `poke__poke` | 需授权；只限当前会话，自动参与拒绝，必须明确请求或叫到机器人 |
| 有情绪、回复不那么客服腔 | `emotion` 插件 + `emotion__read_mood` | 安装并启用；勾选 `read_mood` 后注入情绪语气提示，不改变工具权限 |
| 主动给正在聊天的群友发临时会话 | `emotion` 插件自动轮询 / `emotion__send_whisper` | 默认只挑最近和机器人说过话、正在群聊的人；有额度与冷却；显式工具需授权，自动参与拒绝 |
| 记住近期群聊、回查过去 | 群聊记忆、摘要、`agent__search_history` | 默认可用（可按群关闭） |
| 记住群友本人说过的事实/偏好并回查 | `memory__remember` / `recall` / `forget`，命令 `/记忆`、`/忘记` | 需授权；按账号+群/私聊+用户隔离，只记本人；默认 50 条/200 字/180 天；写工具声明 `memory.write`，自动参与由宿主沙盒拒绝 |
| 人格、模型路由、长回复转图片/转发 | Chat 设置与人格库 | 默认可用 |
| 私聊聊天与工具 | `agent` 私聊智能体（默认关闭，独立全局工具白名单） | 按配置；私聊历史与群记忆隔离，群级设置不适用 |

## 文件与工作区

| 需求 | 工具 | 状态 |
| --- | --- | --- |
| 生成代码/文本并作为附件发送 | `text__send_text_file` | 需授权；明确请求；30k 字符正文，可用 `target_bytes` 补足到 2 MiB |
| 生成 HTML / Markdown 文件 | `text__send_html_file`、`text__send_markdown_file` | 需授权；明确请求 |
| 生成 Word / Excel / PPT 文件 | `docs__write_docx` / `write_xlsx` / `write_pptx` | 需授权；明确请求；受控结构、严格文件名清洗、自动参与拒绝 |
| 长文本转图片、架构图 | `text__render_image`、`text__render_diagram` | 需授权 |
| 读写工作区文件 | `workspace__read_file` / `write_file` | 读写分别受 `allow_read` / `allow_write` 控制 |
| 列出、查看、搜索工作区 | `workspace__list_files`、`stat`、`search` | 只读；有界 |
| 建目录、移动、复制、删除 | `workspace__make_dir` / `move` / `copy` / `delete` | 写入能力；自动参与拒绝 |
| 下载网页/二进制到工作区 | `workspace__download` | 需联网 + 写入；受主机与大小限制 |
| 把工作区文件作为附件发送 | `workspace__send_file` | 需读取 + `qq.send_file`；明确请求；默认 5 MB |
| 读取群里的 QQ 文件附件 | `files__read_file` | 只读；只读当前/引用消息 |
| 跨群读取文件 | **默认禁止** | `workspace_scope = "scene"` 按群/私聊隔离 |

## 联网与外部资料

| 需求 | 工具/能力 | 状态 |
| --- | --- | --- |
| 搜索、读网页、天气、时间 | `web__*` | 需授权；主机/大小受沙盒约束 |
| 给群友发邮件（QQ 邮箱） | `email__send_email` | 需授权 + `allow_email`；收件域名默认只允许 `qq.com`，自动参与拒绝 |
| GitHub 仓库/源码/版本/提交/贡献者/Issue/PR/分支/图片卡片 | `github__*` | 需授权；只读公开仓库，`render_card` 另需 `qq.send` |
| 本地 ONI 源码知识库 | `oni_knowledge__*` | 需授权；只读索引与源码 |
| 任意 URL 下载 | `workspace__download` | 需联网+写入；内网/保留地址默认拒绝 |

## 媒体与设备

| 需求 | 工具 | 状态 |
| --- | --- | --- |
| 把文字读出来 | `voice__speak` | 需授权；公开语音服务 |
| 画图：表情包、头像、情绪图 | `imagegen__draw`（或 `/画`） | 需授权；默认本地 Pillow 画机器人猫娘形象，无需配置；远程模型可选；自动参与拒绝 |
| 把文字排版成图片、架构图 | `text__render_image`、`text__render_diagram` | 需授权 |
| 截取宿主屏幕 | `screen__capture_screen` / `capture_screen_file` | **默认关闭**；需超级管理员/群管理 + 明确请求 |

## 刻意不提供或需要额外插件

| 需求 | 原因/替代 |
| --- | --- |
| 执行本机命令、跑测试、编译 | 进程执行拥有宿主权限，不是当前沙盒能隔离的；默认不提供。需要时用独立 CI/容器或专用插件，并单独评审 |
| PDF / Word / Excel / PPT 生成 | Word / Excel / PPT 已由 `docs` 插件提供（受控结构，无模板/图片/公式）；PDF 仍不提供，需要时另做插件 |
| 邮件附件、HTML 邮件、抄送/密送 | `email` 插件只发送纯文本单封邮件；附件与富文本需要单独评审 |
| 解压/压缩 ZIP | 可下载 ZIP，但没有解压工具；避免路径穿越与炸弹风险，需专用实现 |
| 修改机器人配置、插件源码、数据库 | Agent 只调用显式工具，不能直接改宿主；管理操作走管理 API |
| 跨群/跨账号记忆与文件 | 记忆按账号+场景+用户隔离（`memory` 插件），工作区默认 `scene` 隔离；需要共享时显式把 `workspace_scope` 设为 `shared` 并接受隐私影响 |
| 自动给所有人发文件/截屏 | 自动参与拒绝 `qq.send_file`、`fs.write`、`memory.write`、`screen.capture`；必须明确请求 |

## 开启顺序

1. `[sandbox]`：按需打开 `allow_write`、`allow_network`、`allow_email`、`allow_screen` 等；默认只读联网。
2. 插件管理：启用 `text`、`workspace`、`email`、`screen`、`imagegen`、`memory`、`docs` 等插件，并配置好外部凭据
   （`imagegen` 默认本地绘制，无需配置；要画任意画面才需要图片服务地址、密钥与模型；`docs` 需要宿主已安装
   python-docx / openpyxl / python-pptx）。
3. 工具授权：在「智能体 → 允许调用的工具」逐个勾选，插件启用不等于工具授权。
4. 群策略：可用 `sandbox_write`、`sandbox_network`、`sandbox_private_network`、`sandbox_email`、`sandbox_screen` 收紧或放宽。
5. 使用：文件发送、截屏、写入等副作用必须由群友 **@ 或唤醒词明确请求**；自动参与只做只读判断。

## 验证与扩展

新增能力时：

- 在 `on_ai_tool` 声明真实 `capabilities` 与 `effect`；
- 需要文件/联网时通过 `ctx.sandbox`，不要自行打开绝对路径或创建 HTTP 客户端；
- 给拒绝路径补测试（自动参与、无权限、超限、敏感文件）；
- 更新本清单和 [工具目录契约](tool-registry.md)、[执行沙盒](agent-sandbox.md)。

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe scripts/check_architecture.py
```
