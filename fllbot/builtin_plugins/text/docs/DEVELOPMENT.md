# 开发与维护

`plugin.toml` 为元信息来源，`plugin.py` 声明配置及智能体工具，`service.py` 实现本地文本处理。
图片排版实际位于 `fllbot/rendering/`，同时供宿主自动图片回复使用；包内 `code_render.py`、
`markdown_render.py` 仅兼容旧导入路径。公共渲染器使用 markdown-it-py 解析 token，Pillow 绘制行与表格；
禁止将 HTML 交给浏览器执行或加载链接资源。插件专属转换操作继续放在本包 service.py。
不注册命令，不访问网络，不将文本写入插件源码目录；图片通过内存 PNG 和 OneBot Base64 传输。
Pillow 在排版时延迟导入，缺失依赖不影响其他文本工具加载；更新依赖后可使用图片功能。
新增操作时需同步输入输出上限、工具参数枚举、使用说明及 `tests/test_text_plugin.py`。
图片发送必须使用宿主 `send_media_reply` 流程，不能绕过失效会话和延迟副作用检查。
`file_export.py` 负责文件名校验、UTF-8 MD 原文与离线 HTML 导出；所有专属实现保留在本插件内。
`plugin.py` 注册 `send_html_file` / `send_markdown_file`，使用 Base64 file 消息段和同一 `send_media_reply` 流程，
从而保留当前群/私聊路由、权限复查与不确定送达记录，不直接调用上传 API 绕过交付层。
协议文件段见 [NapCat 消息格式](https://napneko.github.io/develop/msg#file)。
离线验证见 `tests/test_text_file_export.py`，不向真实 QQ 账号发送消息。

`render_diagram` 使用 `fllbot/rendering/diagram_render.py`：解析 Mermaid flowchart 子集（TD/LR、节点形状、
边标签、嵌套 subgraph），保留有界解析器和输入限制。`elk_render.py` 测量实际中文字体，
将节点、分组、标签尺寸送到 ELK.js 计算 compound graph 布局，再用 Pillow 绘图。
新增语法需同步 `tests/test_diagram_render.py` 与 README 的支持列表；调整布局后应检查 TD/LR、回边、同层边、
分组嵌套和超限报错，单图限制为 60 节点 / 140 连线 / 3200×4800 像素。

`tree_render.py` 专门绘制目录树行、缩进引导线以及名称/说明两列；`format=tree` 直接进入此模块，
Markdown 代码块由 `code_render.py` 检测树枝后复用同一实现，不修改普通正文排版。
`elk_layout.cjs` 只负责 stdin/stdout JSON 与本地 ELK 0.12.1 调用；通过参数数组启动 Node，
不经过 shell，Windows 隐藏子进程窗口。运行时不联网、不安装依赖，不将输入作为 JavaScript 执行。
超时 25 秒、Node 堆上限 256 MiB。ELK 的嵌套边坐标相对于返回的 `container`，
`collect_geometry` 必须先收集全部容器偏移，再转换连线，不能假定 root.edges 都使用根坐标。
未修改的第三方 bundle、许可及固定版本来源见 `rendering/vendor/elk/`，wheel 包必须包含 JS、CJS 和许可文件。
原自写路由和编号索引布局已移除，普通正文、代码块和发送权限流程不受影响。
回归测试包含 `tests/test_tree_render.py`；`docs/images/layout-*-preview.*` 保存可重绘的视觉样例。
