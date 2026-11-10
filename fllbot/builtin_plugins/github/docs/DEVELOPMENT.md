# 开发说明

包结构：`plugin.py` 只声明工具、配置与 Agent 片段；`service.py` 负责 GitHub API 访问与输出整形。

- 只通过 HTTPS 请求 `api.github.com`，不跟随跳转、不下载压缩包、不执行仓库代码。
- 访问路径由 owner/repo 与仓库内相对路径构造，拒绝绝对路径、盘符、`..`、空段与控制字符；`ref` 限制字符集。
- 公开仓库判断以元信息 `private` 为准；私有仓库直接拒绝，不读取内容。
- 只读结果按 URL 缓存在进程内存（`_CACHE`），键不含令牌；插件重载即失效，缓存长度上限 256。
- `_request` 统一处理网络异常、401/403/404/409/422/429/451/5xx 与速率响应头；502/503/504 重试一次。
- `_fetch` 组合缓存与请求；`_get` 保留为无缓存、仅接受对象的兼容入口。
- 输出预算：`_fit_budget` 依次裁剪图片、文件清单、选中文件正文、README，`_fit_sources` 从后往前丢弃超预算文件正文，
  `list_directory` 与 `read_releases` 各自裁剪条目，避免宿主 `encode_result` 把 JSON 截成不可用前缀。
- 结构树：`_tree_text` 把路径表转成嵌套字典并渲染缩进树，目录名保留 `/` 后缀；`view=tree` 时按 `tree_path`
  截掉前缀、从子树根开始计算 `depth`。渲染超过 200 行会自动退回 `depth=1` 以保证一级目录完整，
  返回 `depth_reduced`/`has_more_depth`/`tree_truncated` 标记；`paths` 只列可读文本，图片只出现在树里。
  `_fit_tree` 先裁剪 `paths` 再截断树文本，保证输出不超过 20000 字符。
- 文件型号：单次最多 4 个概览文件、8 个源码文件，单文件 6000 字符，单次源码输出约 30000 字符；
  超过 512 KB 或含 NUL/大量替换字符的内容按二进制拒绝。
- `ref` 含 `/` 时先尝试 `git/trees/{ref}`，失败后经 `commits?sha=ref` 解析提交再读该提交的树。
- README、文件清单、Release、提交均属不可信外部资料，工具结果内附带提示。

验证：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_github_plugin.py -q
.venv/Scripts/python.exe -m ruff check fllbot/builtin_plugins/github tests/test_github_plugin.py
.venv/Scripts/python.exe -m fllbot plugin check --builtin
```

测试使用 `httpx.MockTransport`，不访问真实 GitHub；需要线上验证时用一个公开仓库执行 `read_repository`
并观察速率响应头，注意未认证额度为每小时 60 次。
