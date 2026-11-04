# 开发与维护

`plugin.toml` 为元信息来源，`plugin.py` 只声明配置及智能体工具，`service.py` 实现本地处理和有界图片排版。
`markdown_render.py` 使用 markdown-it-py 解析 token，Pillow 绘制行与表格；禁止将 HTML 交给浏览器执行或加载链接资源。
不注册命令，不访问网络，不将文本写入插件源码目录；图片通过内存 PNG 和 OneBot Base64 传输。
Pillow 在排版时延迟导入，缺失依赖不影响其他文本工具加载；更新依赖后可使用图片功能。
新增操作时需同步输入输出上限、工具参数枚举、使用说明及 `tests/test_text_plugin.py`。
图片发送必须使用宿主 `send_media_reply` 流程，不能绕过失效会话和延迟副作用检查。
