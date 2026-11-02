"""Previewed data cleanup jobs and opt-in scheduled retention."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .storage import cleanup

if TYPE_CHECKING:
    from .app import FLLBot

Dataset = Literal['messages', 'conversations', 'usage', 'traces', 'changes']


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    datasets: list[Dataset] = Field(min_length=1, max_length=5)
    older_than_days: int = Field(ge=1, le=3650)
    scene: Literal['group', 'private'] | None = None
    scene_id: int | None = Field(default=None, gt=0)
    self_id: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def validate_scope(self):
        self.datasets = list(dict.fromkeys(self.datasets))
        if self.scene_id is not None and self.scene is None:
            raise ValueError('指定会话号时必须选择群聊或私聊')
        if self.self_id is not None and set(self.datasets) - {'messages', 'traces'}:
            raise ValueError('会话、用量和配置变更记录未按 Bot 隔离；仅消息与诊断支持指定 Bot')
        return self

    def specification(self) -> dict[str, Any]:
        cutoff = time.time() - self.older_than_days * 86400
        return {'cutoffs': dict.fromkeys(self.datasets, cutoff), 'scene': self.scene,
                'scene_id': self.scene_id, 'self_id': self.self_id}


class CleanupManager:
    def __init__(self, bot: FLLBot) -> None:
        self.bot = bot
        self.previews: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.task: asyncio.Task | None = None
        self.scheduler: asyncio.Task | None = None
        self.active = False
        self.last_automatic: dict[str, Any] | None = None

    async def preview(self, request: CleanupRequest) -> dict[str, Any]:
        spec = request.specification()
        current = await cleanup.preview(self.bot.storage, spec)
        preview_id = uuid.uuid4().hex
        result = {**current, 'id': preview_id, 'expires_at': time.time()+600, 'specification': spec}
        self.previews[preview_id] = result
        if len(self.previews) > 20:
            self.previews.pop(next(iter(self.previews)))
        return {key: value for key, value in result.items() if key != 'signature'}

    def _start(self, spec: dict[str, Any], expected: str | None, kind: str) -> dict[str, Any]:
        if self.active:
            raise RuntimeError('已有数据清理正在执行')
        self.active = True
        job_id = uuid.uuid4().hex
        job = {'id': job_id, 'kind': kind, 'status': 'pending', 'started_at': time.time()}
        self.jobs[job_id] = job
        if len(self.jobs) > 50:
            self.jobs.pop(next(iter(self.jobs)))
        self.task = asyncio.create_task(self._run(job, spec, expected), name='fllbot-data-cleanup')
        return dict(job)

    def start(self, preview_id: str) -> dict[str, Any]:
        if self.active:
            raise RuntimeError('已有数据清理正在执行')
        preview = self.previews.get(preview_id)
        if preview is None or preview['expires_at'] < time.time():
            raise ValueError('预览不存在或已过期，请重新预览')
        if not preview['can_execute']:
            raise ValueError('没有可清理数据或数量超过上限，请缩小范围后重新预览')
        self.previews.pop(preview_id)
        return self._start(preview['specification'], preview['signature'], 'manual')

    def automatic_specification(self) -> dict[str, Any]:
        policy = self.bot.config.retention
        return {'cutoffs': {key: time.time()-getattr(policy, key+'_days')*86400
                            for key in ('messages', 'conversations', 'usage', 'changes') if getattr(policy, key+'_days')},
                'scene': None, 'scene_id': None, 'self_id': None}

    def run_automatic(self) -> dict[str, Any]:
        spec = self.automatic_specification()
        if not self.bot.config.retention.enabled or not spec['cutoffs']:
            raise ValueError('自动清理未开启或未设置保留期限')
        return self._start(spec, None, 'automatic')

    async def _run(self, job: dict[str, Any], spec: dict[str, Any], expected: str | None) -> None:
        job['status'] = 'running'
        try:
            result = await cleanup.execute(self.bot.storage, spec, expected=expected, kind=job['kind'],
                                           batch=5000 if job['kind'] == 'automatic' else None)
            job.update(status='completed', result=result)
        except cleanup.CleanupChangedError as exc:
            job.update(status='conflict', error=str(exc))
        except Exception:
            job.update(status='failed', error='清理失败，事务已回滚；请检查运行日志')
            self.bot.logger.exception('数据清理失败')
        finally:
            self.active = False
            job['finished_at'] = time.time()
            if job['kind'] == 'automatic':
                self.last_automatic = dict(job)

    def start_scheduler(self) -> None:
        if self.scheduler is None or self.scheduler.done():
            self.scheduler = asyncio.create_task(self._schedule(), name='fllbot-retention-scheduler')

    async def _schedule(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(60)
            policy = self.bot.config.retention
            if policy.enabled and not self.active and time.monotonic()-last >= policy.interval_seconds:
                last = time.monotonic()
                if self.automatic_specification()['cutoffs']:
                    self.run_automatic()

    async def runs(self, before: int | None = None, limit: int = 20) -> dict[str, Any]:
        cursor = await self.bot.storage.db.execute('SELECT * FROM cleanup_runs WHERE (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?', (before, before, limit+1))
        rows = await cursor.fetchall()
        items = [{**dict(row), 'specification': json.loads(row['specification']), 'counts': json.loads(row['counts'])} for row in rows[:limit]]
        return {'items': items, 'next_cursor': items[-1]['id'] if len(rows)>limit else None, 'active': self.active,
                'last_automatic': self.last_automatic}

    async def close(self) -> None:
        if self.scheduler is not None:
            self.scheduler.cancel()
            await asyncio.gather(self.scheduler, return_exceptions=True)
        if self.task is not None:
            await asyncio.shield(self.task)
