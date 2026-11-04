"""Bounded message diagnostics. Never retain message text, arguments or exception strings."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_active: contextvars.ContextVar[MessageTrace | None] = contextvars.ContextVar('fllbot_message_trace', default=None)
_label = re.compile(r'^[a-zA-Z0-9_.:-]{1,120}$')
STAGES = {'receive', 'queue', 'dispatch', 'access', 'plugin', 'rule', 'permission', 'cooldown',
          'handler', 'trigger', 'model', 'tool', 'send', 'protocol_send', 'memory', 'dedup'}


class MessageTrace:
    def __init__(self, event: Any, connection: Any) -> None:
        self.trace_id = uuid.uuid4().hex
        self.self_id = int(event.self_id or connection.self_id or 0)
        self.scene = 'group' if event.is_group else 'private'
        self.scene_id = int((event.group_id if event.is_group else event.user_id) or 0)
        self.message_id = int(event.message_id or 0)
        self.started_at = time.time()
        self._started = time.perf_counter()
        self.steps: list[dict[str, Any]] = []
        self.truncated = False
        self.failed = False
        self.sent = False
        self.cancelled = False
        self.handled = False
        self.skipped = False
        self.closed = False
        self.metrics = {'queue_wait_ms':None,'first_model_response_ms':None,'first_delivery_ms':None,
                        'model_retries':0,'model_fallbacks':0}

    def add(self, stage: str, code: str, status: str = 'passed', component: str = '') -> dict[str, Any]:
        step = {'stage': stage if isinstance(stage, str) and stage in STAGES else 'handler',
                'code': code if isinstance(code, str) and _label.fullmatch(code) else 'custom_event',
                'status': status if isinstance(status, str) and status in {'running', 'passed', 'skipped', 'failed', 'cancelled'} else 'passed',
                'component': component if isinstance(component, str) and _label.fullmatch(component) else '',
                'elapsed_ms': round((time.perf_counter() - self._started) * 1000, 2)}
        self.observe(step)
        if len(self.steps) < 100:
            self.steps.append(step)
        else:
            self.truncated = True
        return step

    def observe(self, step: dict[str, Any]) -> None:
        if step['stage'] == 'dispatch' and step['status'] == 'running' and self.metrics['queue_wait_ms'] is None:
            self.metrics['queue_wait_ms'] = step['elapsed_ms']
        if step['stage'] == 'model' and step['code'] == 'response_received' and self.metrics['first_model_response_ms'] is None:
            self.metrics['first_model_response_ms'] = step['elapsed_ms']
        if step['stage'] in {'send','protocol_send'} and step['status'] == 'passed' and self.metrics['first_delivery_ms'] is None:
            self.metrics['first_delivery_ms'] = round(step['elapsed_ms']+step.get('duration_ms',0),2)
        if step['stage'] == 'model' and step['code'] == 'retry_scheduled':
            self.metrics['model_retries'] += 1
        if step['stage'] == 'model' and step['code'] == 'fallback_selected':
            self.metrics['model_fallbacks'] += 1
        self.failed |= step['status'] == 'failed'
        self.cancelled |= step['stage'] == 'dispatch' and step['status'] == 'cancelled'
        self.sent |= step['stage'] in {'send', 'protocol_send'} and step['status'] == 'passed'
        self.handled |= step['stage'] == 'handler' and step['status'] == 'passed'
        self.skipped |= step['stage'] in {'access', 'trigger', 'dedup', 'queue'} and step['status'] == 'skipped'

    def summary(self, outcome: str | None = None) -> dict[str, Any]:
        self.closed = True
        if outcome is None:
            outcome = ('cancelled' if self.cancelled else 'warning' if self.failed and self.sent else 'failed' if self.failed
                       else 'sent' if self.sent else 'skipped' if self.skipped else 'processed' if self.handled else 'skipped')
        return {'trace_id': self.trace_id, 'self_id': self.self_id, 'scene': self.scene, 'scene_id': self.scene_id,
                'message_id': self.message_id, 'started_at': self.started_at,
                'duration_ms': round((time.perf_counter() - self._started) * 1000, 2),
                'outcome': outcome, 'truncated': self.truncated, 'steps': [dict(step) for step in self.steps],
                'metrics':dict(self.metrics)}


@contextmanager
def activate(trace: MessageTrace | None) -> Iterator[None]:
    token = _active.set(trace)
    try:
        yield
    finally:
        _active.reset(token)


def record(stage: str, code: str, status: str = 'passed', component: str = '') -> None:
    trace = _active.get()
    if trace is not None and not trace.closed:
        trace.add(stage, code, status, component)


def current_trace_id() -> str | None:
    trace = _active.get()
    return trace.trace_id if trace is not None and not trace.closed else None


@contextmanager
def span(stage: str, component: str = '') -> Iterator[None]:
    trace = _active.get()
    if trace is not None and trace.closed:
        trace = None
    step = trace.add(stage, 'started', 'running', component) if trace else None
    started = time.perf_counter()
    try:
        yield
    except BaseException as exc:
        if step is not None:
            step.update(status='cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed',
                        code='cancelled' if isinstance(exc, asyncio.CancelledError) else 'operation_failed')
        raise
    else:
        if step is not None:
            step.update(status='passed', code='completed')
    finally:
        if step is not None and trace is not None:
            step['duration_ms'] = round((time.perf_counter() - started) * 1000, 2)
            trace.observe(step)


class TraceRecorder:
    def __init__(self, storage: Any) -> None:
        self.storage = storage
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self.task: asyncio.Task | None = None
        self.dropped = 0
        self.write_failures = 0
        self.writing = False
        self.closed = False

    def start(self) -> None:
        self.closed = False
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._write(), name='fllbot-diagnostic-writer')

    def submit(self, trace: MessageTrace, outcome: str | None = None) -> None:
        if self.closed:
            self.dropped += 1
            return
        try:
            self.queue.put_nowait(trace.summary(outcome))
        except asyncio.QueueFull:
            self.dropped += 1

    async def _write(self) -> None:
        while True:
            item = await self.queue.get()
            self.writing = True
            try:
                await self.storage.record_message_trace(item)
            except Exception:
                self.write_failures += 1
                logging.getLogger('fllbot.tracing').warning('消息诊断记录写入失败（累计 %d）', self.write_failures)
            finally:
                self.writing = False
                self.queue.task_done()

    async def flush(self) -> None:
        await self.queue.join()

    async def close(self) -> None:
        self.closed = True
        if self.task is None:
            return
        try:
            await asyncio.wait_for(self.flush(), timeout=5)
        except TimeoutError:
            self.dropped += self.queue.qsize() + int(self.writing)
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()

    def status(self) -> dict[str, int]:
        return {'pending': self.queue.qsize() + int(self.writing), 'capacity': self.queue.maxsize,
                'dropped': self.dropped, 'write_failures': self.write_failures}
