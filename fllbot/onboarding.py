"""Read-only first-use guidance based on the selected bot and group policy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .policy import effective_policy

if TYPE_CHECKING:
    from .app import FLLBot


def setup_guide(bot: FLLBot, self_id: int | None = None, group_id: int | None = None) -> dict[str, Any]:
    accounts = bot.bots.summaries()
    if self_id is None:
        primary = bot.manager.primary()
        self_id = primary.self_id if primary else accounts[0]['self_id'] if accounts else None
    connection = bot.manager.by_self_id(self_id) if self_id else None
    policy = effective_policy(bot, group_id, self_id) if group_id else None
    selected_provider = policy['provider'] if policy else bot.bots.defaults(self_id).provider or bot.config.llm.route(scene='group', task='agent')
    provider = next((item for item in bot.chat.client.providers_status() if item['name'] == selected_provider), None)
    persona = policy['persona'] if policy else bot.bots.defaults(self_id).persona or bot.config.llm.persona
    persona_exists = bool(persona and bot.personas.find(persona))
    steps = [
        {'id': 'connection', 'title': '连接 QQ 账号', 'done': bool(connection and connection.online), 'optional': False,
         'detail': '所选账号已连接' if connection and connection.online else '将系统设置中的反向 WebSocket 地址填入 NapCat，再回来刷新检查。', 'target': 'system'},
        {'id': 'model', 'title': '配置模型服务', 'done': bool(bot.config.llm.enabled and provider and provider['configured']), 'optional': False,
         'detail': f'当前选择：{selected_provider}。配置检查不验证实际连通性，请使用对话测试确认；测试会产生模型用量，不发送 QQ 消息。', 'target': 'models'},
        {'id': 'persona', 'title': '选择人格（选填）', 'done': persona_exists, 'optional': True,
         'detail': f'当前使用：{persona}' if persona_exists else '可在人格库创建并设为默认；跳过时使用兜底提示词。', 'target': 'personas'},
        {'id': 'access', 'title': '选择测试群并允许访问', 'done': bool(group_id and bot.permissions.group_enabled(group_id)), 'optional': False,
         'detail': f'检查群 {group_id} 的访问与总开关。' if group_id else '填写一个测试群号，在群聊设置中确认访问范围和群总开关。', 'target': 'groups'},
        {'id': 'response', 'title': '开启并检查群聊回复', 'done': bool(policy and policy['configured']), 'optional': False,
         'blocked_codes': policy['blocked_codes'] if policy else [],
         'detail': '配置检查通过；请继续真实群验证。' if policy and policy['configured'] else '；'.join(policy['blocked_by']) if policy else '选择测试群后，检查回复方式、AI 开关和插件。', 'target': 'groups'},
    ]
    required = [step for step in steps if not step['optional']]
    return {
        'self_id': self_id, 'group_id': group_id, 'accounts': accounts, 'steps': steps,
        'configuration_ready': all(step['done'] for step in required),
        'completed_checks': sum(step['done'] for step in required), 'total_checks': len(required),
        'verification': {'status': 'manual', 'title': '最后到真实群里验证',
                         'detail': '在所选群发送一条符合回复方式的消息，确认收到 QQ 回复。这里不会自动发送消息，也不会把静态检查当成真实回复验证。'},
        'note': '本引导只读取连接和配置；不请求模型、不执行工具、不验证群成员关系，不修改任何设置。',
    }
