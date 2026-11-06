"""Live agent controls scoped to the requesting account, conversation and sender."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActiveTask:
    ctx: Any
    task: asyncio.Task
    pending: deque = field(default_factory=deque)
    seen: set[str] = field(default_factory=set)
    step: int = 0
    phase: str = 'preparing'
    accepting: bool = True


class AgentTasks:
    def __init__(self, storage, conversations, permissions, scene_enabled, account_enabled):
        self.storage = storage
        self.conversations = conversations
        self.permissions = permissions
        self.scene_enabled = scene_enabled
        self.account_enabled = account_enabled
        self.active: dict[tuple, ActiveTask] = {}
        self.acknowledgements: set[asyncio.Task] = set()

    async def reply(self, ctx, text: str, deferred: bool):
        async def send():
            try:
                with self.conversations.track_task(ctx):
                    await ctx.reply(text, at_sender=True)
            except Exception:
                ctx.trace_event('agent', 'control_reply_failed', 'failed')
        if not deferred:
            await send()
        elif len(self.acknowledgements) < 32:
            task = asyncio.create_task(send(), name='fllbot-task-control-reply')
            self.acknowledgements.add(task)
            task.add_done_callback(self.acknowledgements.discard)

    @staticmethod
    def key(ctx):
        return ctx.self_id, ctx.event.scene, ctx.event.scene_id, ctx.user_id

    @contextmanager
    def track(self, ctx):
        key = self.key(ctx)
        state = ActiveTask(ctx, asyncio.current_task())
        self.active[key] = state
        try:
            yield
        finally:
            if self.active.get(key) is state:
                self.active.pop(key, None)

    def state(self, ctx):
        state = self.active.get(self.key(ctx))
        return state if state and not state.task.done() and state.ctx.data_current else None

    def pending(self, ctx) -> bool:
        state = self.state(ctx)
        return bool(state and state.pending)

    async def drain(self, ctx) -> list[dict]:
        state = self.state(ctx)
        if not state:
            return []
        messages = list(state.pending)
        state.pending.clear()
        if messages:
            await self.conversations.project_input(ctx)
        for message in messages:
            await self.storage.add_chat_message(ctx.event.scene, ctx.event.scene_id, 'user', message['content'],
                user_id=ctx.user_id, nickname=ctx.nickname, sender_role=ctx.sender.role, self_id=ctx.self_id,
                generation=ctx.data_epoch, account_epoch=ctx.received.account_epoch, scope_epoch=ctx.received.scope_generation)
        return messages

    def finish_input(self, ctx) -> None:
        state = self.state(ctx)
        if state:
            state.accepting = False
            state.phase = 'finished'

    async def progress(self, ctx, step: int, phase: str, status: str, component: str = '', details=None):
        state = self.state(ctx)
        if state:
            state.step, state.phase = step, phase
        await self.storage.record_agent_progress(ctx, step, phase, status, component, details)

    async def handle(self, ctx, *, defer_reply: bool = False) -> bool:
        text = ctx.text.strip()
        action, content = '', ''
        if text in {'/任务取消', '取消任务', '停止任务', '不用做了'}:
            action = 'cancel'
        elif text in {'/任务进度', '任务进度'}:
            action = 'status'
        else:
            for prefix in ('/任务补充 ', '补充：', '补充:', '修改要求：', '改成：'):
                if text.startswith(prefix):
                    action, content = 'steer', text[len(prefix):].strip()
                    break
        if not action:
            return False
        for segment in ctx.message:
            if segment.type == 'at':
                if str(segment.get('qq')) not in {str(ctx.self_id), 'all'}:
                    return False
            elif not segment.is_text or segment.text_content.strip():
                break
        async def reply(text):
            await self.reply(ctx, text, defer_reply)
        if (not ctx.is_group or not ctx.data_current or not self.account_enabled(ctx.self_id)
                or not self.permissions.group_enabled(ctx.group_id)
                or not self.permissions.plugin_enabled('agent', ctx.group_id)
                or not self.scene_enabled(ctx.event.scene, ctx.event.scene_id)):
            return False
        state = self.state(ctx)
        if state is None:
            if not text.startswith('/') and not ctx.to_me:
                return False
            await reply('你在当前会话没有正在执行的智能体任务。')
            return True
        identity = ctx.turn_id
        if identity in state.seen:
            return True
        state.seen.add(identity)
        if action == 'cancel':
            accepted = self.conversations.cancel(state.ctx.turn_id, 'user_cancelled')
            await reply('已取消后续处理，已经执行的操作不会撤回。' if accepted else '任务已经结束。')
        elif action == 'status':
            phases = {'preparing': '准备上下文', 'model': '请求模型', 'tool': '执行工具',
                      'step': '处理步骤', 'compaction': '整理任务上下文', 'finished': '准备发送结果',
                      'steering': '等待应用补充要求'}
            await reply(f'任务 {state.ctx.turn_id[:8]}：第 {state.step} 步，'
                        f'{phases.get(state.phase, "处理中")}。')
        elif not state.accepting:
            await reply('当前任务已进入结果发送阶段，请等结果发送后再提出新要求。')
        elif not content or len(content) > 4000 or len(state.pending) >= 8:
            await reply('补充内容需为 1–4000 字；最多同时等待处理 8 条补充。')
        else:
            state.pending.append({'role': 'user', 'content': content,
                '_fllbot_source': {'kind': 'task_steering', 'message_id': ctx.message_id}})
            await self.progress(state.ctx, state.step, 'steering', 'queued', details={'message_id': ctx.message_id})
            await reply('已收到补充，将在下一个安全步骤应用；已经执行的操作不会重做。')
        return True
