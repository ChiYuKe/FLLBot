"""Managed database backup files and bounded in-process backup jobs."""

from __future__ import annotations

import asyncio
import re
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import resolve_path
from .storage import SCHEMA_VERSION
from .storage.backup import validate_database

if TYPE_CHECKING:
    from .app import FLLBot

BACKUP_NAME = re.compile(r'^fllbot-\d{8}-\d{6}(?:-(?:\d{9}|[a-f0-9]{32}))?\.db$')


class BackupBusyError(RuntimeError):
    pass


class BackupManager:
    def __init__(self, bot: FLLBot) -> None:
        self.bot = bot
        self.active = False
        self.task: asyncio.Task | None = None
        self.jobs: dict[str, dict[str, Any]] = {}
        self.verified: dict[str, tuple[int, int, int, dict[str, int]]] = {}

    @property
    def directory(self) -> Path:
        return resolve_path(self.bot.root, self.bot.config.storage.backup_dir).resolve()

    def file(self, name: str) -> Path:
        if not BACKUP_NAME.fullmatch(name):
            raise ValueError('无效的备份文件名')
        path = self.directory / name
        if path.is_symlink() or path.resolve().parent != self.directory or path.resolve() == self.bot.storage.path.resolve():
            raise ValueError('备份路径不在允许范围内')
        if not path.is_file():
            raise FileNotFoundError('备份文件不存在')
        return path

    def _remember(self, path: Path, metadata: dict[str, int]) -> None:
        stat = path.stat()
        self.verified[str(path)] = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, metadata)
        if len(self.verified) > 200:
            self.verified.pop(next(iter(self.verified)))

    def index(self, *, before: str | None = None, limit: int = 30) -> dict[str, Any]:
        if before is not None and not BACKUP_NAME.fullmatch(before):
            raise ValueError('无效的备份分页游标')
        names = sorted((path.name for path in self.directory.glob('fllbot-*.db') if BACKUP_NAME.fullmatch(path.name)), reverse=True)
        if before:
            names = [name for name in names if name < before]
        items = []
        for name in names:
            try:
                path = self.file(name)
                stat = path.stat()
            except (ValueError, OSError):
                continue
            cached = self.verified.get(str(path))
            valid = bool(cached and cached[:3] == (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
            items.append({'name': name, 'size_bytes': stat.st_size, 'modified_at': stat.st_mtime,
                          'validation': 'valid' if valid else 'not_checked',
                          'schema_version': cached[3]['schema_version'] if valid else None})
            if len(items) > limit:
                break
        return {'items': items[:limit], 'next_cursor': items[limit - 1]['name'] if len(items) > limit else None,
                'active': self.active, 'scope': 'database',
                'note': '数据库备份包含会话和数据库内的配置（可能含密钥）；不包含 config.toml、人格文件、插件源码/文件数据或外部模型。'}

    def _claim(self) -> None:
        if self.active:
            raise BackupBusyError('已有备份正在执行，请等待完成')
        self.active = True

    async def _create(self) -> dict[str, Any]:
        name = f"fllbot-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}.db"
        destination = self.directory / name
        metadata = await self.bot.storage.backup(destination)
        self._remember(destination, metadata)
        try:
            display_path = str(destination.relative_to(self.bot.root))
        except ValueError:
            display_path = str(destination)
        return {'ok': True, 'name': name, 'path': display_path, **metadata}

    async def create_now(self) -> dict[str, Any]:
        job = self.start()
        record = self.jobs[job['id']]
        await asyncio.shield(self.task)
        finished = record
        if finished['status'] != 'completed':
            raise OSError(finished.get('error', '数据库备份未完成'))
        return dict(finished['result'])

    def start(self) -> dict[str, Any]:
        self._claim()
        job_id = uuid.uuid4().hex
        job = {'id': job_id, 'status': 'pending', 'started_at': time.time()}
        self.jobs[job_id] = job
        if len(self.jobs) > 50:
            self.jobs.pop(next(iter(self.jobs)))
        self.task = asyncio.create_task(self._run(job), name='fllbot-backup-job')
        return dict(job)

    async def _run(self, job: dict[str, Any]) -> None:
        job['status'] = 'running'
        try:
            job['result'] = await self._create()
            job['status'] = 'completed'
        except Exception:
            job.update(status='failed', error='数据库备份失败，请检查备份目录权限、磁盘空间和运行日志')
            self.bot.logger.exception('数据库备份作业失败')
        finally:
            job['finished_at'] = time.time()
            self.active = False

    async def validate(self, name: str) -> dict[str, Any]:
        path = self.file(name)
        metadata = await asyncio.to_thread(validate_database, path, max_schema_version=SCHEMA_VERSION)
        self._remember(path, metadata)
        return {'name': name, 'validation': 'valid', **metadata}

    def restore_guide(self, name: str) -> dict[str, Any]:
        source = self.file(name)

        def quote(value: str | Path) -> str:
            return "'" + str(value).replace("'", "''") + "'"

        return {'name': name, 'requires_shutdown': True,
                'steps': ['先校验所选备份，并保留当前数据库及人格、插件等文件。',
                          '停止 FLLBot；确认桌面宿主没有自动重启它。',
                          '在 PowerShell 执行下方恢复命令；恢复会校验备份并自动保存当前数据库副本。',
                          '重新启动，检查机器人、群设置和会话；人格与插件文件需另外恢复。'],
                'command': f'& {quote(sys.executable)} -m fllbot restore --root {quote(self.bot.root)} --source {quote(source)} --confirm'}

    async def close(self) -> None:
        if self.task is not None:
            await asyncio.shield(self.task)
