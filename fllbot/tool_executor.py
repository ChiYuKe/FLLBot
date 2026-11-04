"""Host-owned tool validation, authorization, bounded execution and durable facts."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import time
import uuid
from typing import Any

from jsonschema import Draft202012Validator


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
    def __init__(self, storage: Any, permissions: Any) -> None:
        self.storage = storage
        self.permissions = permissions
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
        def failure(code: str) -> tuple[str, bool]:
            ctx.trace_event('tool', code, 'failed', name)
            return json.dumps({'error': code}, ensure_ascii=False), False

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
                    or live_spec.parameters != spec.parameters or live_spec.effect != spec.effect):
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
        except Exception:
            return failure('invalid_arguments')
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
        # Persist before any callback; permission may change while committing.
        if not allowed():
            await self.storage.finish_tool(execution_id, 'cancelled')
            return failure('tool_permission_denied')
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
            return failure('tool_' + status)
        finally:
            ctx.plugin = previous
            ctx.tool_execution_id = ''
        if execution_ctx.stopped:
            ctx.stop()
        # Callback success is a fact even if formatting/model continuation later fails.
        await self.storage.finish_tool(execution_id, 'succeeded')
        output = encode_result(result, spec.max_output_chars)
        silent = bool(isinstance(result, dict) and result.get('silent'))
        await self.storage.finish_tool(execution_id, 'succeeded', output=output, silent=silent,
                                       generation=ctx.data_epoch, account_epoch=ctx.received.account_epoch, scope_epoch=ctx.received.scope_generation)
        return output, silent
