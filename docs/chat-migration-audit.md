# 聊天架构迁移验收核对

本表按 chat-architecture.md 第 11 节核对。实现与验收完成：逐项自动验证、真实 QQ 群聊服务端链路及协议回查均通过，用户已确认客户端只收到一次；不能用测试总数代替逐项验收。

| 验收场景 | 当前证据 | 验证结论与边界 |
| --- | --- | --- |
| 1 重复上报与账号隔离 | test_turns、test_storage、test_account_state | 已验证：test_chat_architecture_e2e 经真实 raw 入口完成两个账号同消息 ID、重复上报、触发/输入/模型/工具/回执与 trace/turn 的全链路关联；test_ingress_facts 补充重启持久去重与无稳定 ID 的边界 |
| 2 有序、并发、拥塞、过期 | test_conversation_queue、test_dispatcher、test_unified_capacity | 已验证：QQ/调试/恢复共享容量与排队时限，接收至业务轮次沿用相同 deadline/代次；test_ingress_facts 验证入队后外部引用修改无法改变会话和输入，公平排队/拥塞/取消/过期由专项测试覆盖 |
| 3 全部上下文范围隔离 | test_storage、test_group_memory、test_quoted_messages | 管理目录、读取与清空已按账号隔离，legacy 明确标注；运行期精确清理代次已按会话隔离并持久化；清理时回执元数据保留已验证，正文擦除后禁止恢复 |
| 4 触发原因可解释 | test_trigger_policy、test_agent_triggers、test_message_traces | 全部入口使用结构化决策；接收/排队丢弃也与轮次事实关联 |
| 5 参数校验与权限撤销 | test_tool_executor、test_plugin_master_switch | test_tool_action_gates 验证插件/群/白名单/身份权限/账号/清空/断线变化会阻止后续协议动作，包含撤权前创建但撤权后才等待的协程；test_voice_delivery 覆盖语音合成后的设置撤销；test_settings_coordination 补充群配置提交后取消仍发布最新权限及重置插件覆盖，配置对象外部修改不能改变已提交快照 |
| 6 工具恢复不重复动作 | test_agent_runtime、test_tool_executor | 内置及随附工具已逐项标注 effect，查询超时为 failed、写操作超时为 uncertain；当前外部写工具未声称支持幂等，模型不同 call_id 重复请求的语义不推断为幂等；语音工具等待任务实际结束，分段持久化回执，成功才投影历史；不再依赖工具自报 spoken 证明送达；部分失败/取消/关闭已验证 |
| 7 模型边界 | test_llm_reliability、test_llm、test_context_builder | 任务局部配置、人设、智能体规则与工具定义已进入同一不可变快照；test_context_contract 验证冻结边界、备用模型独立窗口/图片/工具/输出预算及超预算前置拒绝；快照版本与无正文预算元数据持久化至轮次，清理及重启后可核对 |
| 8 分段与不确定状态 | test_chat_output、test_delivery_recovery、test_storage、test_voice_delivery | ReplyPlan 已持久化引用/@/分段格式；转发不确定禁止恢复，明确拒绝后的普通降级分段可恢复；缺少有效 message_id 的普通/转发/媒体/恢复响应一律 uncertain，不投影历史、不降级或重发；test_e2e 经实际本机 WebSocket 验证有效/缺失回执及未知 echo 隔离 |
| 9 清理与停用 | test_account_state、test_account_write_races、test_retention | 群/插件/隐私摘要提交竞态及关闭期间入队不回填已验证；清理中的回执保留已验证；test_scope_epochs 补充直接清空及保留期限清理提交后重复取消，仍先发布代次再放行排队写入；提交失败正文和代次一并回滚且可重试；停服取消、终态先于数据库关闭、入口拒绝与同实例重启已验证；吞掉取消的异步回调已验证有界返回、过期上下文保护和滞留槽位上限；不强杀任意进程内代码 |
| 10 超长输入与结果 | test_llm_reliability、test_group_memory、test_quoted_messages | 工具结果的 JSON 转义及截断元数据已计入上限，并验证存储/重复调用复用同一有界结果；摘要和原文有宿主来源标记，最终裁剪先移除旧原文再缩减摘要；来源字段在网络边界剥离，当前输入不静默截断；test_context_contract 覆盖规则/工具纳入快照及备用模型实际请求；转发展开已收口至共用上下文构建，来源标为当前附件且遵守身份开关；test_forwards 覆盖普通入口、缓存、总时限、全局节点上限及账号不匹配拒绝；引用缓存同样在上下文失效后禁止复用 |

模块职责与逐项自动验收对应如下：ContextAssembler 已接管提示与上下文组装，ContextBuilder 接管不可变快照，ModelGateway 接管请求/额度/用量并委托 LLMClient 执行协议、重试、备用和最终预算；app.py 显式装配，各组件不反向依赖 ChatService。ChatService 保留兼容入口与对话协调；ReplyService 已独立且冻结回复格式；DeliveryService 已统一文字/媒体/合并转发与受控恢复，llm/output.py 仅保留兼容转交；AgentConversationService 接管触发事实、轮次、历史起点和输入投影协调，agent 插件仅保留接入、配置与工具注册。历史起点已按账号/群隔离；架构检查禁止发送服务依赖模型、agent 接入直接访问存储。
管理调试已接入持久化轮次并提供真实账号只读历史视角；turn_messages 已原子保存 QQ 输入，模型用量与轮次关联；会话身份由各表显式键表达；接收事件、触发、轮次、输入、模型、工具、回执与 trace 的关联已由 raw 入口端到端测试验证。
工具/轮次诊断具备只读接口和安全的普通分段恢复。test_execution_config 已补充排队期间工具白名单与轮数设置变更：旧轮次保持冻结工具定义和限制，新轮次使用新设置，执行时实时撤权仍阻止动作；有效配置哈希包括账号/群策略、默认值及 agent 设置，并与上下文版本一致。

配置写回边界已检查全局配置、Bot 默认、群设置、插件设置与资源替换入口：均先持久化再发布缓存，发布收尾不受调用方取消打断；群首次登记、局部修改、整组保存与恢复继承已补充取消测试。启动字段由 test_startup_config 验证混合请求原子拒绝；max_pending_per_conversation 与已构造队列容量一致，归为启动字段。docs/admin-scope.md 补齐容量、时限及诊断正文开关，scripts/check_admin_scope.py 已纳入 verify.ps1，防止新字段漏记。

诊断隐私由 test_diagnostic_privacy 验证：模型 HTTP/协议/网络错误不回显上游正文；OneBot 非法 JSON、未知数据与处理异常只记录元数据；管理员令牌不进入日志。message_log 默认不保存正文，显式 capture_message_content 开启后受 message_content_hours（1–168 小时，默认 24 小时）约束，启动及每分钟清除过期正文并保留统计行。正式聊天历史与记忆仍遵守各自采集/清理策略，不能用诊断开关代替。

自动验证仅使用模拟模型、模拟 OneBot 与临时数据库；不在测试中发送真实 QQ 消息。

最新完整验证：scripts/verify.ps1 通过，全量 pytest 587 passed（1 条第三方 Starlette 弃用警告），架构、配置字段清单、内置及随附插件包、全仓 Ruff 均通过。测试总数仅记录本次运行，不代替上表的逐项验收；真实 QQ 联调证据见下文。

协议补充验收：test_e2e 使用临时 uvicorn 和真实本机 WebSocket 走完整模型聊天链路（模型与 NapCat 均模拟），覆盖双账号同消息 ID、同账号重复、未知 echo、有效及缺失回执。test_chat_output 对普通分段/合并转发/媒体逐一验证空响应、缺少字段、null、布尔值、空字符串与零值都不能成为 sent；test_delivery_recovery 验证缺失回执后不得再次恢复。test_message_traces 补充非消息动作不产生回复送达指标。该证据覆盖真实适配器和网络收发，但不代表真实 QQ 客户端已经接收。

## 规范正文的实现与证据入口

本表补充第 1–10 节的职责和契约要求，避免只核对第 11 节的十个示例。源码与测试均指当前工作树；后续变动必须重新核对受影响行。

| 规范要求 | 实现入口 | 可核对的验证证据 |
| --- | --- | --- |
| 第 1–2 节：单进程插件兼容、QQ/调试共用业务服务、分开记录接收/生成/工具/送达 | app.py 装配、conversation_service.py、agent_conversation.py、api/routes.py、storage/db.py | test_chat_architecture_e2e 经 raw 入口核对事实关联；test_debug_turns 验证只读账号预览、模型失败与共用取消；调试默认不发送、不执行协议工具、不写正式历史 |
| 第 3 节：服务边界与显式依赖 | context_assembly.py、context_builder.py、model_gateway.py、agent_runtime.py、tool_executor.py、llm/reply.py、delivery_service.py | test_model_components、test_delivery_service 在无整棵应用依赖下执行；test_architecture 与 check_architecture.py 限制模块依赖；agent 插件只转交宿主协调服务 |
| 第 4 节：会话键、轮次、快照、工具与回执契约，legacy 不猜账号 | conversation.py、trigger_policy.py、storage/db.py、llm/context_builder.py、llm/reply.py | test_turn_facts、test_context_contract、test_reply_plan、test_history_scope、test_storage；账号字段和持久唯一键、范围查询、旧数据 self_id=0 均有专项验证 |
| 第 5 节：顺序与公平、有界容量、接收时限、取消与回收 | event_queue.py、conversation.py、conversation_service.py、dispatcher.py | test_conversation_queue、test_unified_capacity、test_scope_epochs、test_account_state；入队复制由 test_ingress_facts 验证；清理提交后取消与重复取消由本轮 test_scope_epochs 验证 |
| 第 6 节：单一上下文构建、标注来源、固定历史截止、预算、后台摘要与送达投影 | llm/context_assembly.py、llm/context_builder.py、llm/budget.py、llm/memory.py、delivery_service.py | test_context_contract、test_group_memory、test_memory_policy_races、test_forwards、test_quoted_messages、test_chat_architecture_e2e；原文/摘要来源在网络边界剥离，部分失败后的上下文只含已送达部分 |
| 第 7 节：工具 schema、实时授权、次数/时限/大小、持久执行与 uncertain | plugin/base.py、tool_executor.py、llm/agent_runtime.py | test_tool_executor、test_tool_action_gates、test_tool_noncooperative、test_tool_categories、test_agent_runtime、test_voice_delivery；成功调用复用结果，不确定调用不自动重跑 |
| 第 8 节：持久化后发送、原账号复核、分段事实、明确拒绝降级与受控恢复 | delivery_service.py、delivery_policy.py、storage/db.py | test_chat_output、test_delivery_service、test_delivery_recovery、test_clear_delivery_facts；重启 sending 转 uncertain，恢复不调用模型或工具 |
| 第 9 节：迁移/索引/事务清理、唯一配置解析、冻结版本和最新权限、观测与诊断隐私 | storage/db.py、storage/cleanup.py、policy.py、llm/execution_config.py、tracing.py、api/console.py | test_storage、test_retention、test_execution_config、test_settings_coordination、test_turn_admin、test_message_traces、test_diagnostic_privacy；配置字段清单由 check_admin_scope.py 检查 |
| 第 10 节：P0/P1/P2 迁移阶段与旧插件 API 兼容 | 同上服务与 llm/chat.py、llm/output.py 兼容门面 | scripts/verify.ps1 对全仓、内置与随附插件和全部自动测试执行检查；兼容入口参与原有聊天、权限、记忆与清理回归 |

真实 QQ 联调另外记录所用 Bot 账号、测试会话、输入/回执消息 ID、turn_id、trace_id 和实际观察结果；模拟 OneBot 不能证明真实网络与客户端行为。实际账号、会话及消息标识保存在本机忽略目录 data/chat-live-acceptance.json，不写入共享文档。

最新只读联调准备检查（2026-11-04）：实际健康接口为 GET /api/health；本机 6701 返回 started=true、connections=1。运行进程以 -m fllbot 启动，--root 指向当前 FLLBot 工作树；管理账号目录确认一个账号在线且启用。此前使用 /health 得出的服务不可达结论作废，受限环境的连接错误也不能单独证明服务未运行。

现有两条历史真实 QQ 送达记录早于本次进程启动，不作为新实现验收证据。用户已指定测试群，并在 QQ 发出“只回复：架构验收 OK”。新轮次 delivered：单个输入事实、一个模型请求、一个回复计划和一个带 message_id 的 sent 回执，候选与唯一可见回答均精确匹配“架构验收 OK”；接收事件 completed，事件/轮次/trace/账号/会话全部关联一致，快照与轮次的有效配置版本一致。排队约 11 ms、首次完整模型响应约 3664 ms、首次送达约 4077 ms，没有模型重试或备用切换，没有执行工具。运行进程启动后相关源码没有修改。

通过现有管理接口调用只读 OneBot get_msg，已从实际 NapCat/QQ 回查发送消息，核对 message_id、发送账号、群号及文本四项全部一致。用户随后明确确认“是，只收到一次”。本机验收记录保存这些检查结果和用户确认，不保存模型密钥或额外聊天正文。

真实联调范围是一个在线账号的群聊成功链路；双账号、私聊、工具副作用及故障竞态按第 11 节要求由模拟模型、模拟 OneBot 和临时数据库验收，没有在真实群重放故障或副作用场景。检查过程中未启动/重启实例、修改运行配置或主动发送 QQ 消息。

完成结论：第 1–10 节实现与边界要求、第 11 节十项模拟验收、项目完整验证命令及最后的真实 QQ 联调均有对应证据。本规范范围内没有剩余必做项；未来的短窗口消息合并、永久偏好体系及微服务拆分不属于本次实现范围。
