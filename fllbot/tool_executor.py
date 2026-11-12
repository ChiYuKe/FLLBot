"""Host-owned tool validation, authorization, bounded execution and durable facts."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import time
import uuid
from dataclasses import replace
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from .plugin.agent import ToolContent, ToolEvent, ToolRejection


def parse_arguments(raw: Any, spec: Any) -> dict:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError('工具参数必须是 JSON 对象')
    schema = dict(spec.parameters)
    # Callback arguments are always closed even when the model schema omits the flag.
    schema['additionalProperties'] = False
    Draft202012Validator(schema).validate(raw)
    return raw


def encode_result(result: Any, limit: int) -> str:
    """Bound the serialized result, including JSON escaping and truncation metadata."""
    encoder = json.JSONEncoder(ensure_ascii=False, default=str)
    pieces = []
    size = 0
    for chunk in encoder.iterencode(result):
        pieces.append(chunk[:limit + 1 - size])
        size += len(chunk)
        if size > limit:
            break
    prefix = ''.join(pieces)
    if size <= limit:
        return prefix
    low, high = 0, len(prefix)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = encoder.encode({'truncated': True, 'preview': prefix[:middle]})
        if len(candidate) <= limit:
            low = middle
        else:
            high = middle - 1
    return encoder.encode({'truncated': True, 'preview': prefix[:low]})


class ExecutionConnection:
    """Guard deferred protocol calls made through a tool's supplied connection."""
    def __init__(self, connection: Any, context: Any) -> None:
        self.connection = connection
        self.context = context

    def __getattr__(self, name: str):
        value = getattr(self.connection,name)
        if not callable(value):
            return value
        if inspect.iscoroutinefunction(value):
            async def async_call(*args, **kwargs):
                if not self.context.data_current:
                    raise RuntimeError('tool_execution_expired')
                return await value(*args, **kwargs)
            return async_call
        def call(*args,**kwargs):
            if not self.context.data_current:
                raise RuntimeError('tool_execution_expired')
            return value(*args,**kwargs)
        return call


class ToolExecutor:
    def __init__(self, storage: Any, permissions: Any, sandbox: Any = None) -> None:
        self.storage = storage
        self.permissions = permissions
        self.sandbox = sandbox
        self.closing = False
        self.max_callbacks = 32
        self._callbacks: dict[asyncio.Task, Any] = {}
        self._executions: set[asyncio.Task] = set()

    def status(self) -> dict:
        return {'callbacks':len(self._callbacks),'capacity':self.max_callbacks,
                'expired_callbacks':sum(bool(ctx.execution_cancelled) for ctx in self._callbacks.values()),
                'requests':len(self._executions),'closing':self.closing}

    def reopen(self) -> None:
        if self._executions:
            raise RuntimeError('Cannot reopen tools with active execution requests')
        self.closing = False

    async def close(self) -> None:
        self.closing = True
        tasks = [task for task in self._executions if task is not asyncio.current_task() and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)
        for task,context in list(self._callbacks.items()):
            context.execution_cancelled = True
            task.cancel()
        # A callback may swallow cancellation; never await it indefinitely.
        await asyncio.sleep(0)

    def _settled(self, task: asyncio.Task) -> None:
        self._callbacks.pop(task,None)
        if not task.cancelled():
            task.exception()  # Consume late errors without persisting a stale result.

    def _hooks(self, ctx: Any, name: str) -> tuple:
        selected = []
        for owner in sorted(ctx.bot.plugins.loaded(), key=lambda plugin: plugin.name):
            if not self.permissions.plugin_enabled(owner.name, ctx.group_id):
                continue
            for hook in sorted(getattr(owner, 'tool_hooks', ()), key=lambda item: item.name):
                targets = {tool if '__' in tool else f'{owner.name}__{tool}' for tool in hook.tools}
                if not targets or name in targets:
                    selected.append((owner, hook))
        return tuple(selected)

    async def _stage(self, ctx: Any, hooks: tuple, stage: str, event: ToolEvent) -> ToolEvent:
        for owner, hook in hooks:
            if not ctx.data_current:
                return event
            if hook.stage != stage:
                continue
            # Unloading or disabling an extension revokes it immediately.
            if (not any(plugin is owner for plugin in ctx.bot.plugins.loaded())
                    or not self.permissions.plugin_enabled(owner.name, ctx.group_id)):
                continue
            try:
                if self.closing or len(self._callbacks) >= self.max_callbacks:
                    raise RuntimeError('tool_executor_busy')
                budget = min(hook.timeout, ctx.received.monotonic_deadline - time.monotonic())
                if budget <= 0:
                    raise TimeoutError('tool_deadline_exceeded')
                hook_ctx = copy.copy(ctx)
                task = asyncio.create_task(hook.callback(event), name=f'fllbot-hook-{owner.name}-{hook.name}')
                self._callbacks[task] = hook_ctx
                task.add_done_callback(self._settled)
                try:
                    done, _ = await asyncio.wait({task}, timeout=budget)
                    if not done:
                        raise TimeoutError('tool_hook_timeout')
                    value = task.result()
                finally:
                    hook_ctx.execution_cancelled = True
                    if not task.done():
                        task.cancel()
                if stage == 'before' and isinstance(value, ToolRejection):
                    return replace(event, status='rejected', content=str(value.reason)[:1000])
                if stage == 'after' and isinstance(value, ToolContent) and isinstance(value.content, str):
                    event = replace(event, content=value.content)
                elif value is not None:
                    raise TypeError('invalid_tool_hook_result')
            except asyncio.CancelledError:
                raise
            except Exception:
                ctx.trace_event('tool_hook', 'hook_failed', 'failed', f'{owner.name}__{hook.name}')
                if stage != 'completed':
                    raise
        return event

    async def execute(self, ctx: Any, name: str, raw: Any, registry: dict,
                      refresh: Any = None) -> tuple[str, bool]:
        task = asyncio.current_task()
        self._executions.add(task)
        try:
            return await self._execute(ctx,name,raw,registry,refresh)
        finally:
            self._executions.discard(task)

    async def _execute(self, ctx: Any, name: str, raw: Any, registry: dict,
                      refresh: Any = None) -> tuple[str, bool]:
        def failure(code: str, details: dict | None = None) -> tuple[str, bool]:
            ctx.trace_event('tool', code, 'failed', name)
            payload = {'error': code}
            if details is not None:
                payload['details'] = details
            return encode_result(payload, 2000), False

        if self.closing:
            return failure('service_stopping')
        if len(self._callbacks) >= self.max_callbacks:
            return failure('tool_executor_busy')
        entry = registry.get(name)
        if entry is None:
            return failure('tool_unavailable')
        owner, spec = entry
        previous = ctx.plugin
        ingress_plugin = previous.name if previous else ''
        ingress_permission = ctx.handler.permission if ctx.handler else None
        def allowed() -> bool:
            live = refresh(ctx)[1].get(name) if refresh is not None else (owner,spec)
            if live is None:
                return False
            live_owner, live_spec = live
            if (live_owner is not owner or live_spec.callback is not spec.callback
                    or live_spec.parameters != spec.parameters or live_spec.effect != spec.effect
                    or live_spec.capabilities != spec.capabilities or live_spec.risk != spec.risk):
                return False
            return (ctx.data_current and ctx.received.monotonic_deadline > time.monotonic()
                    and not getattr(ctx.connection, '_closed', False)
                    and int(getattr(ctx.connection, 'self_id', 0) or 0) == ctx.self_id
                    and (self.permissions.group_enabled(ctx.group_id) if ctx.is_group else self.permissions.allow_private)
                    and (not ingress_plugin or self.permissions.plugin_enabled(ingress_plugin, ctx.group_id))
                    and self.permissions.check(ingress_permission, ctx)
                    and (ingress_plugin != 'agent' or ctx.bot.chat.scene_enabled(ctx.event.scene, ctx.event.scene_id))
                    and self.permissions.plugin_enabled(owner.name, ctx.group_id)
                    and self.permissions.check(spec.permission, ctx)
                    and self.permissions.check(live_spec.permission,ctx))
        if not allowed():
            return failure('tool_permission_denied')
        try:
            arguments = parse_arguments(raw, spec)
        except ValidationError as exc:
            # Exclude instance/message: they can contain private argument values.
            return failure('invalid_arguments', {
                'path': list(exc.absolute_path), 'constraint': exc.validator,
                'expected': exc.validator_value,
            })
        except json.JSONDecodeError as exc:
            return failure('invalid_arguments', {
                'constraint': 'json', 'line': exc.lineno, 'column': exc.colno,
                'hint': '请提供合法的 JSON 对象',
            })
        except ValueError:
            return failure('invalid_arguments', {'constraint': 'object', 'hint': '工具参数必须是 JSON 对象'})
        except Exception:
            return failure('invalid_arguments')
        arguments = copy.deepcopy(arguments)
        # 沙盒裁决先于任何钩子和回调；拒绝不会产生外部动作，也不消耗本轮额度。
        sandbox_runtime = None
        sandbox_verdict = None
        if self.sandbox is not None:
            sandbox_runtime = self.sandbox.runtime(ctx)
            sandbox_verdict = sandbox_runtime.check(spec, arguments)
            sandbox_runtime.note(spec, sandbox_verdict)
            if not sandbox_verdict.allowed:
                ctx.trace_event('tool', 'tool_blocked', 'failed', name)
                return encode_result(sandbox_verdict.payload(), 2000), False
        hooks = self._hooks(ctx, name)
        event = ToolEvent(name, ctx.turn_id, ctx.tool_call_id,
                          arguments=json.dumps(arguments, ensure_ascii=False, sort_keys=True))
        try:
            event = await self._stage(ctx, hooks, 'before', event)
        except Exception:
            return failure('tool_hook_failed')
        if event.status == 'rejected':
            return failure('tool_rejected', {'reason': event.content})
        if not allowed():
            return failure('tool_permission_denied')
        execution_id = uuid.uuid4().hex
        scene = 'group' if ctx.is_group else 'private'
        call_id = ctx.tool_call_id or uuid.uuid4().hex
        fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        claimed, record = await self.storage.claim_tool(
            execution_id, ctx.self_id, scene, ctx.group_id if ctx.is_group else ctx.user_id,
            ctx.message_id or 0, name, spec.effect, ctx.data_epoch, ctx.turn_id, call_id, fingerprint)
        if not record:
            return failure('context_cleared')
        if not claimed:
            if record['tool'] != name or record['arguments_hash'] != fingerprint:
                return failure('call_identity_conflict')
            if record['status'] == 'succeeded' and record['content'] is not None:
                return record['content'], bool(record['silent'])
            return failure('tool_' + record['status'])
        event = replace(event, execution_id=execution_id, call_id=call_id)
        # Persist before any callback; permission may change while committing.
        if not allowed():
            await self.storage.finish_tool(execution_id, 'cancelled')
            return failure('tool_permission_denied')
        if sandbox_runtime is not None:
            # 认领期间配置可能被修改；真正执行前按最新策略再裁决一次。
            refreshed = sandbox_runtime.check(spec, arguments)
            if not refreshed.allowed:
                await self.storage.finish_tool(execution_id, 'cancelled')
                sandbox_runtime.note(spec, refreshed)
                ctx.trace_event('tool', 'tool_blocked', 'failed', name)
                return encode_result(refreshed.payload(), 2000), False
            sandbox_verdict = refreshed
        # The durable claim yielded; concurrent requests may have filled the slots.
        # No await between this final check and callback registration.
        if self.closing or len(self._callbacks) >= self.max_callbacks:
            await self.storage.finish_tool(execution_id, 'cancelled')
            return failure('service_stopping' if self.closing else 'tool_executor_busy')
        execution_ctx = copy.copy(ctx)
        execution_ctx.execution_parent = ctx
        execution_ctx.execution_guard = allowed
        execution_ctx.execution_cancelled = False
        execution_ctx.tool_execution_id = execution_id
        execution_ctx.plugin = owner
        execution_ctx.connection = ExecutionConnection(ctx.connection,execution_ctx)
        if sandbox_runtime is not None and sandbox_verdict is not None:
            sandbox_runtime.commit(spec, sandbox_verdict)
        callback = asyncio.create_task(spec.callback(execution_ctx, **arguments),name='fllbot-tool-'+execution_id)
        self._callbacks[callback] = execution_ctx
        callback.add_done_callback(self._settled)
        try:
            with ctx.trace_span('tool', name):
                budget = max(0.001,min(spec.timeout,ctx.received.monotonic_deadline-time.monotonic()))
                done,_ = await asyncio.wait({callback},timeout=budget)
                if not done:
                    raise TimeoutError('tool_deadline_exceeded')
                result = callback.result()
        except BaseException as exc:
            execution_ctx.execution_cancelled = True
            if not callback.done():
                callback.cancel()
            status = 'failed' if spec.effect == 'read_only' else 'uncertain'
            await self.storage.finish_tool(execution_id, status, type(exc).__name__)
            if isinstance(exc, asyncio.CancelledError):
                raise
            output, quiet = failure('tool_' + status)
            await self._stage(ctx, hooks, 'completed', replace(event, status=status, content=output))
            return output, quiet
        finally:
            ctx.plugin = previous
            ctx.tool_execution_id = ''
        if execution_ctx.stopped:
            ctx.stop()
        # Callback success is a fact even if formatting/model continuation later fails.
        await self.storage.finish_tool(execution_id, 'succeeded')
        silent = bool(isinstance(result, dict) and result.get('silent'))
        try:
            output = encode_result(result, spec.max_output_chars)
            transformed = await self._stage(ctx, hooks, 'after', replace(
                event, status='succeeded', content=output, silent=silent))
            output = transformed.content
            if len(output) > spec.max_output_chars:
                output = encode_result({'content': output}, spec.max_output_chars)
        except Exception:
            # The action remains succeeded; formatting failures must never cause replay.
            output = encode_result({'error': 'tool_result_processing_failed'}, spec.max_output_chars)
        await self.storage.finish_tool(execution_id, 'succeeded', output=output, silent=silent,
                                       generation=ctx.data_epoch, account_epoch=ctx.received.account_epoch, scope_epoch=ctx.received.scope_generation)
        await self._stage(ctx, hooks, 'completed', replace(event, status='succeeded', content=output, silent=silent))
        return output, silent
