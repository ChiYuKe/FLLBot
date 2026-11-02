# 后台管理阶段验收记录

2026-11-03。依据当前源码、隔离测试结果和已保存的浏览器验证记录。此文件记录证据与缺口，不代表整体目标已经完成。最近一次全量回归为 **382 passed**，另有一条依赖弃用提示。

## 阶段核对

| 要求 | 当前证据 | 结论 |
| --- | --- | --- |
| P0 配置项归属、默认值、作用域和生效边界 | admin-scope.md；check_admin_scope.py 检查宿主、模型服务、Bot 与群字段及默认值；源码逐类核对启动读取位置 | 已有清单及检查；新增字段须维护 |
| P0 配置继承与硬限制 | test_group_policy、test_bots、test_account_defaults、test_plugin_master_switch；全局插件明确停用覆盖群 true，声明默认关闭仍允许群 opt-in；隔离预览中切换后显示“全局停用”及群覆盖保留说明 | 后端和独立 Chrome 页面均已验证；页面操作用假数据，切换后恢复预览原态 |
| P0 多窗口与保存语义 | test_admin_changes、test_settings_coordination、test_persona_versions、test_plugin_settings_versions、test_resource_replacement；截图含群/人格/插件配置/账号冲突 | 核心配置入口已验证；旧无条件 API 保持兼容 |
| P0 旧配置与重启 | test_startup_config；旧启动覆盖归档、失败回滚/释放；test_group_memory 旧库升级；账号、资源引用与任务重载测试 | 隔离实例已验证，未操作真实实例迁移 |
| P1 群列表、默认配置、移除覆盖 | test_console、test_group_policy、test_admin_resources、test_admin_changes；全部/单独配置/需检查、Bot 查看视角和共享群覆盖 | 后端与此前浏览器流程有证据 |
| P1 首次接入与不回复排查 | test_onboarding、test_admin_diagnostics、test_message_traces | 配置检查与真实群验证分开；不冒充已发 QQ 测试 |
| P1 资源引用、版本与替换 | test_admin_resources、test_persona_versions、test_resource_replacement；人格冲突与引用替换截图 | 后端主流程及默认人格删除失败恢复已有定向测试；浏览器冲突/替换截图已有 |
| P1 管理记录 | management_changes 的群/全局/Bot/插件配置/插件开关/引用替换/启动归档/人格和插件生命周期元数据；手动清空会话与通用 KV 同事务/脱敏记录测试 | 已覆盖管理操作开始与结果；记录不含人格正文、聊天正文、KV 键值或插件异常正文 |
| P2 消息诊断与日志关联 | test_message_traces 处理阶段、重试/备用、工具、发送、取消、队列边界与隐私测试 | 后端与此前页面验证通过；有界异步写入，不复制正文 |
| P2 外部服务与备份 | test_maintenance；备份下载、校验、停机恢复隔离验证，voice 仅 GET /health；备份截图 | 已有证据；备份范围明确仅主库，不含插件/人格文件 |
| P2 用量与数据保留 | test_usage_dashboard、test_retention、test_group_memory；清理预览、冲突、事务回滚、摘要及旧处理结果失效；私聊范围不会匹配无归属的全局管理审计 | 已有证据；统计只反映保留数据，费用为估算 |
| P3 插件任务与运行记录 | test_daily_plugin、test_task_console；任务中心截图，按账号/场景/创建者隔离、暂停/取消/受控重试和重载恢复 | daily 首批适配已验证；新任务插件按声明契约接入 |
| 管理访问与凭据边界 | test_startup_config 关闭后台/API/文档/事件流且保留 OneBot；test_api 校验 `/ui` 的 no-store/no-cache/no-referrer；app.js 收到引导令牌后缓存并从地址栏移除；独立 Chrome 打开带 token 的隔离预览后地址栏变为 `/ui#plugins`；插件及配置错误脱敏测试 | 后端和独立 Chrome 已验证；桌面宿主人工核对仍待完成 |
| 桌面嵌入和独立浏览器 | 此前 Chrome 独立浏览器截图及导航/表单测试；desktop 样式与主题适配代码 | 独立浏览器已有部分证据；未据此声称原生桌面宿主人工验收通过 |

## 明确未完成项

1. 原生桌面宿主的最终人工核对。独立 Chrome 的插件全局停用状态及带令牌启动后的地址栏均已核验；用户截图已显示 QQBot 桌面外壳内嵌 FLLBot；本次全站布局在隔离 Chrome 验证，尚未在实际桌面外壳完成最后一轮操作核验。

P4 的多人角色、远程部署、多租户和批量实例导入导出仍按原规划按需扩展，不在当前阶段提前建设。未启动真实 QQ、模型或语音服务做验收，未部署到真实运行实例。

## 复核入口

全量测试使用项目虚拟环境运行 pytest；静态门禁为 ruff、scripts/check_architecture.py、scripts/check_admin_scope.py、内置/独立插件检查以及各修改 JS 文件的语法检查。测试位于独立临时数据库和模拟连接中。截图在 docs/images/，文件名对应各已验证流程；没有为被审批拦截的新插件开关流程伪造截图。
