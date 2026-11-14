# 开发说明

包结构：`plugin.py` 只声明工具、配置与 Agent 片段；`service.py` 负责 GitHub API 访问与输出整形。

- 只通过 HTTPS 请求 `api.github.com`，不跟随跳转、不下载压缩包、不执行仓库代码。
- 访问路径由 owner/repo 与仓库内相对路径构造，拒绝绝对路径、盘符、`..`、空段与控制字符；`ref` 限制字符集。
- 公开仓库判断以元信息 `private` 为准；私有仓库直接拒绝，不读取内容。
- 只读结果按 URL 缓存在进程内存（`_CACHE`），键不含令牌（代码搜索另把 Accept 并入缓存键）；插件重载即失效，缓存长度上限 256。
- `_request` 统一处理网络异常、401/403/404/409/422/429/451/5xx 与速率响应头；502/503/504 重试一次。
- `_fetch` 组合缓存与请求；`_get` 保留为无缓存、仅接受对象的兼容入口。
- 输出预算：`_fit_budget` 依次裁剪图片、文件清单、选中文件正文、README，`_fit_sources` 从后往前丢弃超预算文件正文，
  `list_directory` 与 `read_releases` 各自裁剪条目，避免宿主 `encode_result` 把 JSON 截成不可用前缀。
  `_fit_list` 从列表尾部裁剪 Issue/PR/贡献者/分支/提交文件，`_fit_thread` 先删评论再缩短正文；各工具预算见
  `_CONTRIBUTOR_BUDGET`、`_ISSUE_BUDGET`、`_THREAD_BUDGET`、`_COMMIT_BUDGET`、`_BRANCH_BUDGET`、`_CODE_SEARCH_BUDGET`。
- 贡献者：`GET /contributors` 只返回按提交数排序的账号，`anon=1` 时附带匿名提交（无 `login`，只有 name/email，
  不要拿名字冒充账号）；接口统计中会返回 202 且正文为空，`_request` 对空正文返回 `None`，`_fetch` 不缓存空响应，
  `read_contributors` 据此返回 `state=pending` 并提示稍后重试，而不是报网络错误。
- Issue/PR：`GET /issues` 的结果混有 PR，`read_issues` 按 `pull_request` 字段过滤，并一次扫描 3 倍目标条数
  （上限 100）避免结果页几乎全是 PR；`page` 透传给 API，`next_page` 仅在整页取满时给出。
  `read_issue` 先取 `/issues/{number}`，是 PR 时再取 `/pulls/{number}` 补合并状态与增删统计；评论总数已知，
  按 `ceil(total/limit)` 直接取最后一页，因此只保留最近的讨论，`comments_truncated` 标记是否还有更早评论。
- 提交与分支：`read_commit` 的 `ref` 不是 7–40 位十六进制时，先经 `commits?sha=` 解析成 SHA 再取详情，
  规避分支名含 `/` 的路径编码问题；补丁受 `_MAX_PATCH_PER_FILE`（单文件）与 `_MAX_PATCH_TOTAL`（单次）约束，
  被截断的文件带 `patch_truncated`/`patch_skipped`。`read_branches` 读取 `/branches` 的 name/protected/commit.sha。
- 代码搜索：GitHub 代码搜索必须认证，未配置令牌时 `search_code` 在发请求前直接报错；查询会剥掉模型写的
  `repo:` 限定符再追加目标仓库，避免跨仓库搜索。请求使用 `application/vnd.github.text-match+json`，
  `_fetch` 把 Accept 并入缓存键，避免与普通 JSON 响应混用；搜索额度单独计数。
- 图片卡片：`render.py` 只用 Pillow 本地绘制，不联网；`icons.py` 用圆、线、多边形等图元画出
  星标/Fork/Issue/PR/提交/分支/文件等图标，不依赖 emoji、图标字体或外部图片。
  `_Builder` 收集「高度 + 绘制函数」行块，最后一次性算出画布尺寸并绘制；`_wrap` 保留显式换行、
  按中英混排折行并在超限时加省略号；标题优先加载系统粗体字形，找不到时才用描边模拟。
  `render_card(kind, payload, avatars=...)` 按 `kind` 分发到八个构造函数，`payload` 就是 `service.py`
  只读函数的结果，由 `plugin.py` 的 `_card_payload` 按类型取数；渲染放在 `asyncio.to_thread`，
  异常只返回 `error`，不发送半成品。字体经 `fllbot.rendering.text_image.load_font` 解析。
- 头像：`service.fetch_avatars` 只接受 `https://avatars.githubusercontent.com/...`，追加 `s=96`
  取小图，单张上限 512 KB 且必须返回 `image/*`；最多同时处理 24 张、6 路并发，失败或超限直接跳过，
  渲染层回退为首字母色块。头像在进程内缓存 1 天（`_AVATAR_CACHE`，最多 128 条），不写入磁盘；
  测试通过 `httpx.MockTransport` 模拟，不访问真实头像站。
  发送走 `send_media_reply`（`qq.send`），自动参与会被沙盒拒绝。
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
.venv/Scripts/python.exe -m pytest tests/test_github_plugin.py tests/test_github_render.py -q
.venv/Scripts/python.exe -m ruff check fllbot/builtin_plugins/github tests/test_github_plugin.py tests/test_github_render.py
.venv/Scripts/python.exe -m fllbot plugin check --builtin
```

测试使用 `httpx.MockTransport`，不访问真实 GitHub；需要线上验证时用一个公开仓库执行 `read_repository`
并观察速率响应头，注意未认证额度为每小时 60 次。
