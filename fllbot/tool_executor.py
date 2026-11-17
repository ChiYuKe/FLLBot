"""Host-owned tool validation, authorization, bounded execution and durable facts."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import re
import time
import uuid
from dataclasses import replace
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from .plugin.agent import ToolContent, ToolEvent, ToolRejection

# 只有纯读取类能力的结果才可复用；带 qq.send / fs.write 等能力的一律不缓存。
CACHEABLE_CAPABILITIES = frozenset({"fs.read", "net.read", "memory.read", "system.read"})

# 模型可直接转述的中文失败说明；error/reason 等结构化字段保持不变。
FAILURE_MESSAGES = {
    'service_stopping': '服务正在停止，这次操作没有执行，请稍后再试。',
    'tool_executor_busy': '当前同时进行的工具操作太多，这次没有执行，请稍后再试。',
    'tool_unavailable': '这个工具当前不可用，可能已被卸载或没有开放给本会话。',
    'tool_permission_denied': '当前发言人或本会话没有使用这个工具的权限，这次操作没有执行。',
    'invalid_arguments': '工具参数不符合要求，这次操作没有执行；请按提示修正参数后重试。',
    'tool_hook_failed': '工具的前置检查失败，这次操作没有执行。',
    'tool_rejected': '这次工具调用被扩展的检查规则拒绝，没有执行。',
    'context_cleared': '本轮会话数据已清理，这次操作没有执行。',
    'call_identity_conflict': '同一调用编号对应的参数不一致，这次没有执行；请发起新的调用。',
    'tool_succeeded': '这次调用此前已经成功执行，请直接复用已有结果，不要重复调用。',
    'tool_running': '相同的操作正在执行中，这次没有重复执行，请等待已有结果。',
    'tool_failed': '工具执行失败，这次操作没有完成；请如实说明未完成部分，不要假称已完成。',
    'tool_uncertain': '这次操作的结果不确定，可能已经执行，请不要自动重试；先向用户说明可能已完成。',
    'tool_cancelled': '这次操作已被取消，没有执行。',
    'tool_blocked': '这次操作被安全策略拦下，没有执行。',
    'tool_repeat_limit': '同一轮里相同参数的只读查询次数过多，这次没有执行；请直接复用已有结果或调整参数。',
    'tool_result_processing_failed': '工具已经执行成功，但结果整理失败，请不要重复执行这个动作。',
}
DEFAULT_FAILURE_MESSAGE = '工具调用没有成功，这次操作没有执行或结果无法确认。'
TYPE_NAMES = {
    'integer': '整数', 'number': '数字', 'string': '文本', 'boolean': '布尔值 true/false',
    'array': '数组', 'object': 'JSON 对象', 'null': 'null',
}
_INTEGER_TEXT = re.compile(r'^[+-]?\d+$')
_NUMBER_TEXT = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$')
# 自愈只在有限深度和节点数内进行，避免异常嵌套拖慢校验或循环。
MAX_REPAIR_DEPTH = 8
MAX_REPAIR_NODES = 64


def _schema_type(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ''
    kind = schema.get('type')
    if isinstance(kind, str):
        return kind
    if isinstance(kind, list):
        for item in kind:
            if isinstance(item, str) and item != 'null':
                return item
    return ''


class _ArgumentRepair:
    """把无损可转换的类型错误改成 schema 期望的类型；不做语义猜测。"""

    def __init__(self) -> None:
        self.changes = 0
        self.nodes = 0

    @staticmethod
    def _integer(value: Any) -> tuple[bool, Any]:
        if isinstance(value, str):
            text = value.strip()
            if _INTEGER_TEXT.fullmatch(text):
                return True, int(text)
        elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return True, int(value)
        return False, value

    @staticmethod
    def _number(value: Any) -> tuple[bool, Any]:
        if isinstance(value, str):
            text = value.strip()
            if _NUMBER_TEXT.fullmatch(text):
                number = int(text) if _INTEGER_TEXT.fullmatch(text) else float(text)
                if isinstance(number, float) and not math.isfinite(number):
                    return False, value
                return True, number
        return False, value

    @staticmethod
    def _boolean(value: Any) -> tuple[bool, Any]:
        if isinstance(value, str):
            text = value.strip().casefold()
            if text == 'true':
                return True, True
            if text == 'false':
                return True, False
        return False, value

    @staticmethod
    def _string(value: Any) -> tuple[bool, Any]:
        if isinstance(value, bool):
            return True, 'true' if value else 'false'
        if isinstance(value, int):
            return True, str(value)
        if isinstance(value, float) and math.isfinite(value):
            return True, json.dumps(value)
        return False, value

    def value(self, value: Any, schema: Any, depth: int = 0) -> Any:
        if (depth > MAX_REPAIR_DEPTH or self.nodes >= MAX_REPAIR_NODES
                or not isinstance(schema, dict)):
            return value
        self.nodes += 1
        expected = _schema_type(schema)
        if expected == 'integer':
            ok, coerced = self._integer(value)
            value, self.changes = (coerced, self.changes + 1) if ok else (value, self.changes)
        elif expected == 'number':
            ok, coerced = self._number(value)
            value, self.changes = (coerced, self.changes + 1) if ok else (value, self.changes)
        elif expected == 'boolean':
            ok, coerced = self._boolean(value)
            value, self.changes = (coerced, self.changes + 1) if ok else (value, self.changes)
        elif expected == 'string':
            ok, coerced = self._string(value)
            value, self.changes = (coerced, self.changes + 1) if ok else (value, self.changes)
        elif expected == 'array' and not isinstance(value, list) and value is not None:
            value, self.changes = [value], self.changes + 1
        elif expected == 'object' and not isinstance(value, dict) and isinstance(value, str):
            text = value.lstrip()
            if text.startswith('{'):
                try:
                    decoded = json.loads(value)
                except (TypeError, ValueError):
                    decoded = None
                if isinstance(decoded, dict):
                    value, self.changes = decoded, self.changes + 1
        if isinstance(value, dict):
            properties = schema.get('properties')
            if isinstance(properties, dict):
                for key in list(value):
                    child = properties.get(key)
                    if isinstance(child, dict):
                        value[key] = self.value(value[key], child, depth + 1)
        elif isinstance(value, list):
            items = schema.get('items')
            if isinstance(items, dict):
                for index in range(len(value)):
                    value[index] = self.value(value[index], items, depth + 1)
            elif isinstance(items, list):
                for index, child in enumerate(items):
                    if index < len(value) and isinstance(child, dict):
                        value[index] = self.value(value[index], child, depth + 1)
        return value


def repair_arguments(raw: Any, spec: Any) -> dict | None:
    """按 schema 做有限类型自愈并重新严格校验；无可修正或仍失败时抛 ValidationError。"""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError('工具参数必须是 JSON 对象')
    schema = dict(spec.parameters)
    schema['additionalProperties'] = False
    repaired = copy.deepcopy(raw)
    engine = _ArgumentRepair()
    repaired = engine.value(repaired, schema)
    if not engine.changes:
        return None
    Draft202012Validator(schema).validate(repaired)
    return repaired


def parse_arguments(raw: Any, spec: Any, *, repair: bool = False) -> dict:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError('工具参数必须是 JSON 对象')
    schema = dict(spec.parameters)
    # Callback arguments are always closed even when the model schema omits the flag.
    schema['additionalProperties'] = False
    try:
        Draft202012Validator(schema).validate(raw)
    except ValidationError as exc:
        if not repair:
            raise
        repaired = repair_arguments(raw, spec)
        if repaired is None:
            raise exc
        return repaired
    return raw


def _human_path(path: list) -> str:
    text = ''
    for part in path:
        if isinstance(part, int):
            text += f'[{part}]'
        else:
            text = f'{text}.{part}' if text else str(part)
    return text or '参数对象'


def _bounded_expected(value: Any) -> Any:
    """schema 常量可以回传，但限制长度，避免把大枚举/长模式塞进失败结果。"""
    if isinstance(value, str):
        return value if len(value) <= 120 else value[:120] + '…'
    if isinstance(value, list):
        if len(value) <= 8:
            return [_bounded_expected(item) for item in value]
        return [*[_bounded_expected(item) for item in value[:8]], '…']
    if isinstance(value, dict):
        return {str(key): _bounded_expected(item) for key, item in list(value.items())[:8]}
    return value


def _constraint_summary(exc: ValidationError) -> str:
    validator, expected = exc.validator, exc.validator_value
    if validator == 'type':
        names = expected if isinstance(expected, list) else [expected]
        return '类型应为' + ' 或 '.join(TYPE_NAMES.get(str(name), str(name)) for name in names)
    if validator == 'required':
        names = expected if isinstance(expected, list) else [expected]
        return '必须提供字段：' + '、'.join(str(name) for name in names)
    if validator == 'additionalProperties':
        return '不允许多余字段'
    if validator == 'enum':
        names = expected if isinstance(expected, list) else [expected]
        return '取值必须是：' + '、'.join(str(name) for name in names[:8])
    labels = {
        'minimum': '最小值', 'maximum': '最大值', 'exclusiveMinimum': '最小值（不含）',
        'exclusiveMaximum': '最大值（不含）', 'minLength': '最小长度', 'maxLength': '最大长度',
        'minItems': '最少项数', 'maxItems': '最多项数', 'minProperties': '最少字段数',
        'maxProperties': '最多字段数', 'multipleOf': '必须是它的倍数',
    }
    if validator in labels:
        return f'{labels[validator]} {expected}'
    if validator == 'const':
        return f'值必须固定为 {expected}'
    if validator in {'pattern', 'format'}:
        return '需要符合指定格式'
    if validator == 'uniqueItems':
        return '元素不能重复'
    return f'需要满足约束：{validator}'


def _repair_hint(exc: ValidationError, field: str) -> str:
    validator = exc.validator
    if validator == 'type':
        expected = exc.validator_value
        names = expected if isinstance(expected, list) else [expected]
        readable = ' 或 '.join(TYPE_NAMES.get(str(name), str(name)) for name in names)
        return f'请把 {field} 改成{readable}；数字不要加引号，布尔值用 true/false。'
    if validator == 'required':
        return '请补齐缺少的必填字段后重试。'
    if validator == 'additionalProperties':
        return '请删除工具参数定义中没有声明的字段后重试。'
    if validator in {'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
                     'minLength', 'maxLength', 'minItems', 'maxItems', 'minProperties',
                     'maxProperties', 'multipleOf'}:
        return f'请把 {field} 调整到允许的范围内后重试。'
    if validator == 'enum':
        return f'请从允许的取值中选择 {field}。'
    if validator in {'pattern', 'format'}:
        return f'请按要求的格式提供 {field}。'
    return f'请按工具参数说明调整 {field} 后重试。'


def _field_schema(spec: Any, path: list) -> dict:
    node = getattr(spec, 'parameters', None)
    for part in path:
        if not isinstance(node, dict):
            return {}
        if isinstance(part, int):
            items = node.get('items')
            if isinstance(items, dict):
                node = items
            elif isinstance(items, list) and 0 <= part < len(items) and isinstance(items[part], dict):
                node = items[part]
            else:
                return {}
        else:
            properties = node.get('properties')
            if isinstance(properties, dict) and isinstance(properties.get(part), dict):
                node = properties[part]
            else:
                return {}
    return node if isinstance(node, dict) else {}


def invalid_argument_details(exc: ValidationError, spec: Any) -> dict:
    """给模型可执行的修正信息：只含 schema 路径与约束，不含任何原始参数值。"""
    path = list(exc.absolute_path)
    field = _human_path(path)
    details = {
        'path': path,
        'field': field,
        'constraint': exc.validator,
        'expected': _bounded_expected(exc.validator_value),
        'expected_summary': _constraint_summary(exc),
        'hint': _repair_hint(exc, field),
    }
    description = _field_schema(spec, path).get('description')
    if isinstance(description, str) and description.strip():
        details['description'] = description.strip()[:200]
    return details


def sandbox_failure_payload(verdict: Any) -> dict:
    """保留沙盒自身的 reason/hint 边界，只补一句可转述的中文说明。"""
    payload = verdict.payload()
    hint = payload.get('hint')
    payload['message'] = (f'这次操作没有执行：{hint}' if isinstance(hint, str) and hint.strip()
                          else FAILURE_MESSAGES['tool_blocked'])
    return payload


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
        self.max_result_cache = 128
        self.result_cache_seconds = 300.0
        self._result_cache: dict[tuple, tuple[float, str, bool]] = {}
        self.cache_hits = 0
        # 同轮相同只读调用的硬上限；计数只保留有限键，避免模型刷参数撑大内存。
        self.repeat_limit = 12
        self.max_repeat_keys = 256
        self._repeat_counts: dict[tuple, int] = {}
        self.repeat_blocked = 0

    def status(self) -> dict:
        return {'callbacks':len(self._callbacks),'capacity':self.max_callbacks,
                'expired_callbacks':sum(bool(ctx.execution_cancelled) for ctx in self._callbacks.values()),
                'requests':len(self._executions),'closing':self.closing,
                'cache_entries':len(self._result_cache),'cache_hits':self.cache_hits,
                'repeat_keys':len(self._repeat_counts),'repeat_blocked':self.repeat_blocked}

    @staticmethod
    def _cacheable(spec: Any) -> bool:
        capabilities = set(getattr(spec, 'capabilities', ()) or ())
        return (getattr(spec, 'effect', '') == 'read_only' and bool(capabilities)
                and capabilities <= CACHEABLE_CAPABILITIES)

    def _cache_get(self, key: tuple) -> tuple[str, bool] | None:
        record = self._result_cache.get(key)
        if record is None:
            return None
        stored, output, silent = record
        if time.monotonic() - stored > self.result_cache_seconds:
            self._result_cache.pop(key, None)
            return None
        self.cache_hits += 1
        return output, silent

    def _cache_put(self, key: tuple, output: str, silent: bool) -> None:
        if len(self._result_cache) >= self.max_result_cache:
            self._result_cache.pop(next(iter(self._result_cache)), None)
        self._result_cache[key] = (time.monotonic(), output, silent)

    def _repeat_blocked(self, ctx: Any, name: str, spec: Any, fingerprint: str) -> bool:
        """只对只读工具计同轮相同参数次数；写/发送/管理类不设此限。"""
        if getattr(spec, 'effect', '') != 'read_only':
            return False
        key = (ctx.turn_id, ctx.self_id, name, fingerprint)
        count = self._repeat_counts.get(key, 0) + 1
        if key not in self._repeat_counts and len(self._repeat_counts) >= self.max_repeat_keys:
            self._repeat_counts.pop(next(iter(self._repeat_counts)), None)
        self._repeat_counts[key] = count
        if count > self.repeat_limit:
            self.repeat_blocked += 1
            return True
        return False

    def reopen(self) -> None:
        if self._executions:
            raise RuntimeError('Cannot reopen tools with active execution requests')
        self.closing = False
        self._result_cache.clear()
        self._repeat_counts.clear()

    async def close(self) -> None:
        self.closing = True
        self._result_cache.clear()
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
                      refresh: Any = None, resolve: Any = None) -> tuple[str, bool]:
        task = asyncio.current_task()
        self._executions.add(task)
        try:
            return await self._execute(ctx,name,raw,registry,refresh,resolve)
        finally:
            self._executions.discard(task)

    async def _execute(self, ctx: Any, name: str, raw: Any, registry: dict,
                      refresh: Any = None, resolve: Any = None) -> tuple[str, bool]:
        def failure(code: str, details: dict | None = None) -> tuple[str, bool]:
            ctx.trace_event('tool', code, 'failed', name)
            payload = {'error': code,
                       'message': FAILURE_MESSAGES.get(code, DEFAULT_FAILURE_MESSAGE)}
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
            if resolve is not None:
                live = resolve(name)
            elif refresh is not None:
                live = refresh(ctx)[1].get(name)
            else:
                live = (owner, spec)
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
            # 先尝试无损类型自愈；修正后仍用 Draft202012Validator 严格校验。
            try:
                repaired = repair_arguments(raw, spec)
            except ValidationError as second:
                return failure('invalid_arguments', invalid_argument_details(second, spec))
            except Exception:
                return failure('invalid_arguments', invalid_argument_details(exc, spec))
            if repaired is None:
                return failure('invalid_arguments', invalid_argument_details(exc, spec))
            arguments = repaired
            # 只记录“发生过修正”和工具名，绝不写原始或修正后的参数值。
            ctx.trace_event('tool', 'arguments_repaired', 'passed', name)
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
        fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # 沙盒裁决先于任何钩子和回调；拒绝不会产生外部动作，也不消耗本轮额度。
        sandbox_runtime = None
        sandbox_verdict = None
        if self.sandbox is not None:
            sandbox_runtime = self.sandbox.runtime(ctx)
            sandbox_verdict = sandbox_runtime.check(spec, arguments)
            sandbox_runtime.note(spec, sandbox_verdict)
            if not sandbox_verdict.allowed:
                ctx.trace_event('tool', 'tool_blocked', 'failed', name)
                return encode_result(sandbox_failure_payload(sandbox_verdict), 2000), False
        # 同轮同参只读调用的硬上限：超过后不执行、不写执行事实、不调用钩子。
        if self._repeat_blocked(ctx, name, spec, fingerprint):
            return failure('tool_repeat_limit')
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
        # 同一轮里重复的纯读取调用直接复用结果，避免重复 I/O、存储写入和钩子开销；
        # 带钩子的工具不缓存，保持钩子对每次调用的裁决能力。
        cache_key = None
        if not hooks and self._cacheable(spec):
            cache_key = (ctx.turn_id, ctx.self_id, name, fingerprint)
            cached = self._cache_get(cache_key)
            if cached is not None:
                ctx.trace_event('tool', 'cache_hit', 'passed', name)
                return cached
        execution_id = uuid.uuid4().hex
        scene = 'group' if ctx.is_group else 'private'
        call_id = ctx.tool_call_id or uuid.uuid4().hex
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
                return encode_result(sandbox_failure_payload(refreshed), 2000), False
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
            output = encode_result({
                'error': 'tool_result_processing_failed',
                'message': FAILURE_MESSAGES['tool_result_processing_failed'],
            }, spec.max_output_chars)
        await self.storage.finish_tool(execution_id, 'succeeded', output=output, silent=silent,
                                       generation=ctx.data_epoch, account_epoch=ctx.received.account_epoch, scope_epoch=ctx.received.scope_generation)
        await self._stage(ctx, hooks, 'completed', replace(event, status='succeeded', content=output, silent=silent))
        if cache_key is not None:
            self._cache_put(cache_key, output, silent)
        return output, silent
