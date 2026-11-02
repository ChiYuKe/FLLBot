"""SQLite 持久化：迁移、键值、群配置、插件状态、聊天历史与统计。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .backup import DatabaseFileLock, create_backup
from .locking import SettingsLock
from .memory import GroupMemoryStore

SCHEMA_VERSION = 9

MIGRATIONS: dict[int, list[str]] = {
    9: [
        """CREATE TABLE cleanup_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
            created_at REAL NOT NULL, specification TEXT NOT NULL, counts TEXT NOT NULL)""",
        'CREATE INDEX idx_chat_created ON chat_messages(created_at)',
        'CREATE INDEX idx_memory_created ON group_memory_messages(created_at)',
        'CREATE INDEX idx_summary_ended ON group_memory_summaries(ended_at)',
        'CREATE INDEX idx_management_changed ON management_changes(created_at)',
    ],
    8: [
        """CREATE TABLE message_traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL UNIQUE,
            self_id INTEGER NOT NULL, scene TEXT NOT NULL, scene_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, started_at REAL NOT NULL, duration_ms REAL NOT NULL,
            outcome TEXT NOT NULL, truncated INTEGER NOT NULL, steps TEXT NOT NULL)""",
        "CREATE INDEX idx_message_traces_scope ON message_traces(self_id, scene, scene_id, id DESC)",
        "CREATE INDEX idx_message_traces_time ON message_traces(started_at)",
    ],
    7: [
        """CREATE TABLE management_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
            action TEXT NOT NULL, fields TEXT NOT NULL, created_at REAL NOT NULL)""",
        "CREATE INDEX idx_management_changes_group ON management_changes(group_id, id DESC)",
        """CREATE TRIGGER group_change_insert AFTER INSERT ON groups BEGIN
            INSERT INTO management_changes(group_id, action, fields, created_at)
            VALUES(NEW.group_id, 'created', '["enabled","config"]', NEW.updated_at);
        END""",
        """CREATE TRIGGER group_change_update AFTER UPDATE ON groups
        WHEN OLD.enabled != NEW.enabled OR OLD.config != NEW.config BEGIN
            INSERT INTO management_changes(group_id, action, fields, created_at)
            SELECT NEW.group_id, 'updated', json_group_array(field), NEW.updated_at FROM (
                SELECT 'enabled' AS field WHERE OLD.enabled != NEW.enabled
                UNION SELECT 'config.' || incoming.key FROM json_each(NEW.config) AS incoming
                    WHERE incoming.value IS NOT (SELECT previous.value FROM json_each(OLD.config) AS previous WHERE previous.key = incoming.key)
                       OR incoming.type IS NOT (SELECT previous.type FROM json_each(OLD.config) AS previous WHERE previous.key = incoming.key)
                UNION SELECT 'config.' || key FROM json_each(OLD.config)
                    WHERE key NOT IN (SELECT key FROM json_each(NEW.config))
            );
        END""",
        """CREATE TRIGGER group_change_delete AFTER DELETE ON groups BEGIN
            INSERT INTO management_changes(group_id, action, fields, created_at)
            VALUES(OLD.group_id, 'deleted', '[]', (julianday('now') - 2440587.5) * 86400.0);
        END""",
        """CREATE TRIGGER global_change_insert AFTER INSERT ON meta
        WHEN NEW.key = 'config_overrides' BEGIN
            INSERT INTO management_changes(group_id, action, fields, created_at)
            VALUES(0, 'global_updated', '["system"]', (julianday('now') - 2440587.5) * 86400.0);
        END""",
        """CREATE TRIGGER global_change_update AFTER UPDATE ON meta
        WHEN NEW.key = 'config_overrides' AND OLD.value != NEW.value BEGIN
            INSERT INTO management_changes(group_id, action, fields, created_at)
            VALUES(0, 'global_updated', '["system"]', (julianday('now') - 2440587.5) * 86400.0);
        END""",
    ],
    6: [
        """CREATE TABLE group_memory_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, self_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
            user_id INTEGER NOT NULL DEFAULT 0, nickname TEXT NOT NULL DEFAULT '',
            sender_role TEXT NOT NULL DEFAULT '', source_id INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL)""",
        "CREATE INDEX idx_group_memory_scope ON group_memory_messages(self_id, group_id, id)",
        """CREATE UNIQUE INDEX idx_group_memory_source ON group_memory_messages(self_id, group_id, source_id)
            WHERE role = 'user' AND source_id != 0""",
        """CREATE TABLE group_memory_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, self_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
            first_id INTEGER NOT NULL, last_id INTEGER NOT NULL, started_at REAL NOT NULL,
            ended_at REAL NOT NULL, privacy TEXT NOT NULL, content TEXT NOT NULL,
            UNIQUE(self_id, group_id, first_id, last_id, privacy))""",
        "CREATE INDEX idx_group_summary_scope ON group_memory_summaries(self_id, group_id, last_id)",
    ],
    5: [
        # Old records could contain estimates without a marker. Treat them conservatively.
        "ALTER TABLE llm_usage ADD COLUMN usage_estimated INTEGER NOT NULL DEFAULT 1",
    ],
    1: [
        """
        CREATE TABLE IF NOT EXISTS kv (
            scope      TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (scope, key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS groups (
            group_id   INTEGER PRIMARY KEY,
            enabled    INTEGER NOT NULL DEFAULT 1,
            config     TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_state (
            plugin     TEXT NOT NULL,
            group_id   INTEGER NOT NULL DEFAULT 0,
            enabled    INTEGER NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (plugin, group_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            scene      TEXT NOT NULL,
            scene_id   INTEGER NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_chat_scene ON chat_messages (scene, scene_id, id DESC)",
        """
        CREATE TABLE IF NOT EXISTS message_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            self_id    INTEGER NOT NULL DEFAULT 0,
            scene      TEXT NOT NULL,
            scene_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            content    TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_message_log_time ON message_log (created_at)",
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    ],
    2: [
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            scene      TEXT NOT NULL,
            scene_id   INTEGER NOT NULL,
            creator    INTEGER NOT NULL,
            content    TEXT NOT NULL,
            due_at     REAL NOT NULL,
            created_at REAL NOT NULL,
            fired      INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (fired, due_at)",
        "CREATE INDEX IF NOT EXISTS idx_reminders_scene ON reminders (scene, scene_id, fired)",
    ],
    3: [
        """
        CREATE TABLE IF NOT EXISTS llm_usage (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            scene             TEXT NOT NULL DEFAULT '',
            scene_id          INTEGER NOT NULL DEFAULT 0,
            provider          TEXT NOT NULL DEFAULT '',
            model             TEXT NOT NULL DEFAULT '',
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms        REAL NOT NULL DEFAULT 0,
            cost              REAL NOT NULL DEFAULT 0,
            ok                INTEGER NOT NULL DEFAULT 1,
            error             TEXT NOT NULL DEFAULT '',
            created_at        REAL NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_llm_usage_time ON llm_usage (created_at DESC)",
    ],
    4: [
        # 记录每条聊天消息的发送者，供群聊上下文还原「谁在说话」。
        "ALTER TABLE chat_messages ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chat_messages ADD COLUMN nickname TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE chat_messages ADD COLUMN sender_role TEXT NOT NULL DEFAULT ''",
    ],
}


class ChatRecord(tuple):
    """一条聊天历史：主体仍是 ``(role, content)``，并附带发送者信息。

    继承自 ``tuple`` 以兼容旧代码的拆包与比较；新代码可用 ``.role``、
    ``.content``、``.user_id``、``.nickname``、``.sender_role`` 读取。
    """

    def __new__(
        cls,
        role: str,
        content: str,
        user_id: int = 0,
        nickname: str = "",
        sender_role: str = "",
        created_at: float = 0,
    ) -> ChatRecord:
        record = super().__new__(cls, (role, content))
        record.user_id = int(user_id or 0)
        record.nickname = nickname or ""
        record.sender_role = sender_role or ""
        record.created_at = float(created_at or 0)
        return record

    @property
    def role(self) -> str:
        return self[0]

    @property
    def content(self) -> str:
        return self[1]


class Storage:
    """单连接 SQLite 封装。所有写入走同一连接，天然串行。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.logger = logging.getLogger("fllbot.storage")
        self._db: aiosqlite.Connection | None = None
        self._process_lock: DatabaseFileLock | None = None
        self.memory = GroupMemoryStore(self)
        self.settings_lock = SettingsLock()
        self.data_lock = SettingsLock()
        self.conversation_generation = 0

    # -- 生命周期 --------------------------------------------------------- #
    async def connect(self) -> None:
        if self._db is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._process_lock = DatabaseFileLock(self.path)
        self._process_lock.acquire()
        try:
            self._db = await aiosqlite.connect(self.path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            await self._db.execute("PRAGMA busy_timeout=5000")
            await self._migrate()
        except BaseException:
            try:
                if self._db is not None:
                    await self._db.close()
                    self._db = None
            finally:
                self._process_lock.release()
                self._process_lock = None
            raise
        self.logger.info("数据库就绪：%s", self.path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
        if self._process_lock is not None:
            self._process_lock.release()
            self._process_lock = None

    async def backup(self, destination: Path | str) -> dict[str, int]:
        """Create a validated, online-safe backup of this database."""
        if self._db is None:
            raise RuntimeError("数据库尚未连接，请先 await storage.connect()")
        return await asyncio.to_thread(
            create_backup, self.path, destination, max_schema_version=SCHEMA_VERSION
        )

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("数据库尚未连接，请先 await storage.connect()")
        return self._db

    async def _migrate(self) -> None:
        cursor = await self.db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        version = int(row[0]) if row else 0
        for target in sorted(MIGRATIONS):
            if target <= version:
                continue
            for statement in MIGRATIONS[target]:
                await self.db.execute(statement)
            await self.db.execute(f"PRAGMA user_version = {target}")
            await self.db.commit()
            self.logger.info("数据库迁移到版本 %d", target)

    async def schema_version(self) -> int:
        cursor = await self.db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # -- 键值 ------------------------------------------------------------- #
    async def kv_get(self, scope: str, key: str, default: Any = None) -> Any:
        cursor = await self.db.execute("SELECT value FROM kv WHERE scope = ? AND key = ?", (scope, key))
        row = await cursor.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    async def kv_set(self, scope: str, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        await self.db.execute(
            "INSERT INTO kv (scope, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (scope, key, payload, time.time()),
        )
        await self.db.commit()

    async def save_plugin_settings(self, scope: str, entries: list[tuple[int, str, Any]]) -> None:
        """Commit a validated plugin settings batch and field-only audit atomically."""
        async with aiosqlite.connect(self.path, timeout=5) as connection:
            try:
                await connection.execute('BEGIN IMMEDIATE')
                fields: dict[int, list[str]] = {}
                now = time.time()
                for group_id, key, value in entries:
                    await connection.execute(
                        'INSERT INTO kv(scope,key,value,updated_at) VALUES(?,?,?,?) '
                        'ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                        (scope, f'{group_id}:{key}', json.dumps(value, ensure_ascii=False), now),
                    )
                    fields.setdefault(group_id, []).append(f'{scope}.{key}')
                for group_id, keys in fields.items():
                    await connection.execute('INSERT INTO management_changes(group_id,action,fields,created_at) VALUES(?,?,?,?)',
                                             (group_id, 'plugin_settings_updated', json.dumps(sorted(keys)), now))
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def save_bot_profile(self, self_id: int, profile: dict[str, Any], fields: list[str]) -> None:
        """Persist one account profile and its field-only audit in one transaction."""
        async with aiosqlite.connect(self.path, timeout=5) as connection:
            try:
                await connection.execute('BEGIN IMMEDIATE')
                now = time.time()
                await connection.execute(
                    "INSERT INTO kv(scope,key,value,updated_at) VALUES('bot-profiles',?,?,?) "
                    'ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                    (str(self_id), json.dumps(profile, ensure_ascii=False), now),
                )
                if fields:
                    await connection.execute('INSERT INTO management_changes(group_id,action,fields,created_at) VALUES(0,?,?,?)',
                                             ('bot_profile_updated', json.dumps([f'bot.{self_id}.{field}' for field in sorted(fields)]), now))
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def kv_delete(self, scope: str, key: str) -> None:
        await self.db.execute("DELETE FROM kv WHERE scope = ? AND key = ?", (scope, key))
        await self.db.commit()

    async def admin_kv_set(self, scope: str, key: str, value: Any) -> None:
        """Write generic admin KV and its value-free audit atomically."""
        async with aiosqlite.connect(self.path, timeout=5) as connection:
            try:
                await connection.execute('BEGIN IMMEDIATE')
                await connection.execute(
                    'INSERT INTO kv(scope,key,value,updated_at) VALUES(?,?,?,?) '
                    'ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                    (scope, key, json.dumps(value, ensure_ascii=False), time.time()),
                )
                await connection.execute(
                    'INSERT INTO management_changes(group_id,action,fields,created_at) VALUES(0,?,?,?)',
                    ('kv_updated', json.dumps([f'scope:{scope}']), time.time()),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def admin_kv_delete(self, scope: str, key: str) -> bool:
        """Delete generic admin KV and audit the changed scope, never its value/key."""
        async with aiosqlite.connect(self.path, timeout=5) as connection:
            try:
                await connection.execute('BEGIN IMMEDIATE')
                cursor = await connection.execute('DELETE FROM kv WHERE scope=? AND key=?', (scope, key))
                deleted = bool(cursor.rowcount)
                if deleted:
                    await connection.execute(
                        'INSERT INTO management_changes(group_id,action,fields,created_at) VALUES(0,?,?,?)',
                        ('kv_deleted', json.dumps([f'scope:{scope}']), time.time()),
                    )
                await connection.commit()
                return deleted
            except BaseException:
                await connection.rollback()
                raise

    async def kv_all(self, scope: str) -> dict[str, Any]:
        cursor = await self.db.execute("SELECT key, value FROM kv WHERE scope = ? ORDER BY key", (scope,))
        rows = await cursor.fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                result[row["key"]] = row["value"]
        return result

    async def kv_scopes(self) -> list[str]:
        cursor = await self.db.execute("SELECT DISTINCT scope FROM kv ORDER BY scope")
        return [row["scope"] for row in await cursor.fetchall()]

    # -- 元数据 ----------------------------------------------------------- #
    async def meta_get(self, key: str, default: Any = None) -> Any:
        cursor = await self.db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    async def meta_set(self, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        await self.db.commit()

    async def archive_startup_overrides(self, active: dict[str, Any], archived: dict[str, Any]) -> dict[str, Any]:
        """Retain legacy startup-only values separately from effective overrides."""
        async with aiosqlite.connect(self.path, timeout=5) as connection:
            try:
                await connection.execute('BEGIN IMMEDIATE')
                row = await (await connection.execute("SELECT value FROM meta WHERE key='inactive_startup_overrides'")).fetchone()
                previous = json.loads(row[0]) if row else {}
                retained = {**previous, **archived}
                for key, value in [('config_overrides', active), ('inactive_startup_overrides', retained)]:
                    await connection.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                             (key, json.dumps(value, ensure_ascii=False)))
                await connection.execute('INSERT INTO management_changes(group_id,action,fields,created_at) VALUES(0,?,?,?)',
                                         ('startup_overrides_archived', json.dumps(sorted(archived)), time.time()))
                await connection.commit()
                return retained
            except BaseException:
                await connection.rollback()
                raise

    # -- 群 --------------------------------------------------------------- #
    async def upsert_group(
        self,
        group_id: int,
        *,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
        clear_settings_scope: str | None = None,
    ) -> None:
        """登记或局部更新群设置；``None`` 表示该字段保持不变。

        使用单条 upsert 语句，避免并发首次登记时撞 UNIQUE 约束。
        """
        now = time.time()
        gid = int(group_id)
        if enabled is None and config is None:
            await self.db.execute(
                "INSERT OR IGNORE INTO groups (group_id, enabled, config, created_at, updated_at) "
                "VALUES (?, 1, '{}', ?, ?)",
                (gid, now, now),
            )
        elif config is None:
            await self.db.execute(
                "INSERT INTO groups (group_id, enabled, config, created_at, updated_at) "
                "VALUES (?, ?, '{}', ?, ?) "
                "ON CONFLICT(group_id) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
                (gid, int(bool(enabled)), now, now),
            )
        elif enabled is None:
            await self.db.execute(
                "INSERT INTO groups (group_id, enabled, config, created_at, updated_at) "
                "VALUES (?, 1, ?, ?, ?) "
                "ON CONFLICT(group_id) DO UPDATE SET config = excluded.config, updated_at = excluded.updated_at",
                (gid, json.dumps(config, ensure_ascii=False), now, now),
            )
        else:
            await self.db.execute(
                "INSERT INTO groups (group_id, enabled, config, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(group_id) DO UPDATE SET "
                "enabled = excluded.enabled, config = excluded.config, updated_at = excluded.updated_at",
                (gid, int(bool(enabled)), json.dumps(config, ensure_ascii=False), now, now),
            )
        if clear_settings_scope:
            await self.db.execute("DELETE FROM kv WHERE scope = ? AND key LIKE ?", (clear_settings_scope, f"{gid}:%"))
        await self.db.commit()

    async def list_group_states(self) -> dict[int, dict[str, Any]]:
        cursor = await self.db.execute("SELECT group_id, enabled, config FROM groups ORDER BY group_id")
        result: dict[int, dict[str, Any]] = {}
        for row in await cursor.fetchall():
            try:
                config = json.loads(row["config"] or "{}")
            except json.JSONDecodeError:
                config = {}
            result[int(row["group_id"])] = {"enabled": bool(row["enabled"]), "config": config}
        return result

    async def group_changes(self, group_id: int, *, before: int | None = None, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        cursor = await self.db.execute(
            "SELECT id, group_id, action, fields, created_at FROM management_changes "
            "WHERE group_id IN (0, ?) AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
            (int(group_id), before, before, limit + 1),
        )
        rows = await cursor.fetchall()
        items = [{**dict(row), "fields": json.loads(row["fields"]), "actor": "instance"} for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    async def record_management_change(self, action: str, fields: list[str], *, group_id: int = 0) -> None:
        """Append metadata-only audit for filesystem and lifecycle operations."""
        await self.db.execute(
            'INSERT INTO management_changes(group_id,action,fields,created_at) VALUES(?,?,?,?)',
            (int(group_id), action, json.dumps(fields, ensure_ascii=False), time.time()),
        )
        await self.db.commit()

    async def record_message_trace(self, trace: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO message_traces(trace_id,self_id,scene,scene_id,message_id,started_at,duration_ms,outcome,truncated,steps) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            tuple(trace[key] for key in ('trace_id', 'self_id', 'scene', 'scene_id', 'message_id', 'started_at',
                                        'duration_ms', 'outcome', 'truncated')) + (json.dumps(trace['steps'], ensure_ascii=False),),
        )
        await self.db.execute("DELETE FROM message_traces WHERE id <= (SELECT max(id) - 10000 FROM message_traces) OR started_at < ?", (time.time() - 7 * 86400,))
        await self.db.commit()

    async def message_traces(self, *, self_id: int | None = None, scene: str | None = None, scene_id: int | None = None,
                             outcome: str | None = None, message_id: int | None = None, before: int | None = None, limit: int = 30) -> dict[str, Any]:
        conditions, params = ['started_at >= ?'], [time.time() - 7 * 86400]
        for column, value in (('self_id', self_id), ('scene', scene), ('scene_id', scene_id), ('outcome', outcome), ('message_id', message_id)):
            if value is not None:
                conditions.append(f'{column} = ?')
                params.append(value)
        if before is not None:
            conditions.append('id < ?')
            params.append(before)
        limit = max(1, min(limit, 100))
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        cursor = await self.db.execute('SELECT id,trace_id,self_id,scene,scene_id,message_id,started_at,duration_ms,outcome,truncated FROM message_traces' + where + ' ORDER BY id DESC LIMIT ?', (*params, limit + 1))
        rows = await cursor.fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {'items': items, 'next_cursor': items[-1]['id'] if len(rows) > limit else None}

    async def message_trace(self, trace_id: str) -> dict[str, Any] | None:
        cursor = await self.db.execute('SELECT * FROM message_traces WHERE trace_id = ? AND started_at >= ?', (trace_id, time.time() - 7 * 86400))
        row = await cursor.fetchone()
        return {**dict(row), 'steps': json.loads(row['steps'])} if row else None

    async def delete_group(self, group_id: int) -> None:
        await self.db.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
        await self.db.commit()

    # -- 插件状态 --------------------------------------------------------- #
    async def set_plugin_state(self, plugin: str, group_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.path, timeout=5) as connection:
            try:
                await connection.execute('BEGIN IMMEDIATE')
                previous = await (await connection.execute('SELECT enabled FROM plugin_state WHERE plugin=? AND group_id=?',
                                                           (plugin, int(group_id)))).fetchone()
                now = time.time()
                await connection.execute(
                    'INSERT INTO plugin_state(plugin,group_id,enabled,updated_at) VALUES(?,?,?,?) '
                    'ON CONFLICT(plugin,group_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at',
                    (plugin, int(group_id), int(bool(enabled)), now),
                )
                if previous is None or bool(previous[0]) != bool(enabled):
                    await connection.execute('INSERT INTO management_changes(group_id,action,fields,created_at) VALUES(?,?,?,?)',
                                             (int(group_id), 'plugin_state_updated', json.dumps([f'plugin.{plugin}.enabled']), now))
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def list_plugin_states(self) -> dict[tuple[str, int], bool]:
        cursor = await self.db.execute("SELECT plugin, group_id, enabled FROM plugin_state")
        return {(row["plugin"], int(row["group_id"])): bool(row["enabled"]) for row in await cursor.fetchall()}

    async def clear_plugin_states(self, plugin: str) -> None:
        await self.db.execute("DELETE FROM plugin_state WHERE plugin = ?", (plugin,))
        await self.db.commit()

    # -- 聊天历史 --------------------------------------------------------- #
    async def add_chat_message(
        self,
        scene: str,
        scene_id: int,
        role: str,
        content: str,
        *,
        user_id: int = 0,
        nickname: str = "",
        sender_role: str = "",
        generation: int | None = None,
    ) -> None:
        """写入一条历史；``role`` 是消息角色（user/assistant），发送者信息另存。"""
        async with self.data_lock:
            if generation is not None and generation != self.conversation_generation:
                return
            await self.db.execute(
                "INSERT INTO chat_messages (scene, scene_id, role, content, user_id, nickname, sender_role, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scene,
                    int(scene_id),
                    role,
                    content,
                    int(user_id or 0),
                    str(nickname or "")[:120],
                    str(sender_role or "")[:20],
                    time.time(),
                ),
            )
            await self.db.commit()

    async def recent_chat(
        self,
        scene: str,
        scene_id: int,
        limit: int = 20,
        *,
        created_after: float | None = None,
    ) -> list[ChatRecord]:
        query = (
            "SELECT role, content, user_id, nickname, sender_role, created_at FROM chat_messages "
            "WHERE scene = ? AND scene_id = ?"
        )
        params: list[Any] = [scene, int(scene_id)]
        if created_after is not None:
            query += " AND created_at >= ?"
            params.append(float(created_after))
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [
            ChatRecord(
                role=row["role"],
                content=row["content"],
                user_id=int(row["user_id"] or 0),
                nickname=row["nickname"] or "",
                sender_role=row["sender_role"] or "",
                created_at=row["created_at"],
            )
            for row in reversed(rows)
        ]

    async def clear_chat(self, scene: str, scene_id: int) -> int:
        async with self.data_lock:
            cursor = await self.db.execute(
                "DELETE FROM chat_messages WHERE scene = ? AND scene_id = ?", (scene, int(scene_id))
            )
            removed = cursor.rowcount or 0
            if scene == "group":
                await self.db.execute("DELETE FROM group_memory_messages WHERE group_id = ?", (int(scene_id),))
                await self.db.execute("DELETE FROM group_memory_summaries WHERE group_id = ?", (int(scene_id),))
            await self.db.execute(
                'INSERT INTO management_changes(group_id,action,fields,created_at) VALUES(?,?,?,?)',
                (int(scene_id) if scene == 'group' else 0, 'chat_history_cleared',
                 json.dumps([f'scene:{scene}', f'messages:{removed}']), time.time()),
            )
            await self.db.commit()
            self.conversation_generation += 1
            return removed

    # -- LLM 用量 --------------------------------------------------------- #
    async def record_llm_usage(
        self,
        *,
        scene: str,
        scene_id: int,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        cost: float = 0.0,
        ok: bool = True,
        error: str = "",
        usage_estimated: bool = False,
    ) -> None:
        await self.db.execute(
            "INSERT INTO llm_usage (scene, scene_id, provider, model, prompt_tokens, completion_tokens,"
            " latency_ms, cost, ok, error, created_at, usage_estimated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scene,
                int(scene_id),
                provider,
                model,
                int(prompt_tokens),
                int(completion_tokens),
                float(latency_ms),
                float(cost),
                1 if ok else 0,
                error[:500],
                time.time(),
                int(usage_estimated),
            ),
        )
        await self.db.commit()

    async def group_llm_usage(self, group_id: int, since: float) -> dict[str, Any]:
        cursor = await self.db.execute(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,"
            " COALESCE(SUM(cost), 0) AS cost FROM llm_usage"
            " WHERE scene = 'group' AND scene_id = ? AND created_at >= ?",
            (int(group_id), since),
        )
        row = await cursor.fetchone()
        return {"tokens": int(row["tokens"]), "cost": float(row["cost"])}

    async def llm_usage_summary(self, since: float) -> dict[str, Any]:
        """聚合某时间点之后的调用次数、token、延迟与费用。"""
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS requests, COALESCE(SUM(ok), 0) AS ok_count,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COALESCE(AVG(latency_ms), 0) AS avg_latency, COALESCE(SUM(cost), 0) AS cost,"
            " COALESCE(SUM(CASE WHEN ok = 1 THEN usage_estimated ELSE 0 END), 0) AS estimated_requests"
            " FROM llm_usage WHERE created_at >= ?",
            (float(since),),
        )
        row = await cursor.fetchone()
        cursor = await self.db.execute(
            "SELECT model, provider, COUNT(*) AS requests,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COUNT(*) - COALESCE(SUM(ok), 0) AS failures,"
            " COALESCE(AVG(latency_ms), 0) AS avg_latency, COALESCE(SUM(cost), 0) AS cost,"
            " COALESCE(SUM(CASE WHEN ok = 1 THEN usage_estimated ELSE 0 END), 0) AS estimated_requests"
            " FROM llm_usage WHERE created_at >= ? GROUP BY model, provider ORDER BY requests DESC, model, provider",
            (float(since),),
        )
        by_model = [
            {
                "model": item["model"],
                "provider": item["provider"],
                "requests": int(item["requests"]),
                "tokens": int(item["tokens"]),
                "prompt_tokens": int(item["prompt_tokens"]),
                "completion_tokens": int(item["completion_tokens"]),
                "failures": int(item["failures"]),
                "avg_latency_ms": round(float(item["avg_latency"]), 1),
                "cost": round(float(item["cost"]), 6),
                "estimated_requests": int(item["estimated_requests"]),
            }
            for item in await cursor.fetchall()
        ]
        requests = int(row["requests"] if row else 0)
        ok_count = int(row["ok_count"] if row else 0)
        prompt = int(row["prompt_tokens"] if row else 0)
        completion = int(row["completion_tokens"] if row else 0)
        return {
            "requests": requests,
            "failures": max(0, requests - ok_count),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "avg_latency_ms": round(float(row["avg_latency"] if row else 0), 1),
            "cost": round(float(row["cost"] if row else 0), 6),
            "estimated_requests": int(row["estimated_requests"] if row else 0),
            "by_model": by_model,
        }

    async def clear_llm_usage(self) -> int:
        cursor = await self.db.execute("DELETE FROM llm_usage")
        await self.db.commit()
        return cursor.rowcount or 0

    async def chat_rooms(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT scene, scene_id, COUNT(*) AS count, MAX(created_at) AS last_at "
            "FROM chat_messages GROUP BY scene, scene_id ORDER BY last_at DESC"
        )
        return [
            {
                "scene": row["scene"],
                "scene_id": int(row["scene_id"]),
                "messages": int(row["count"]),
                "last_at": row["last_at"],
            }
            for row in await cursor.fetchall()
        ]

    # -- 消息统计 --------------------------------------------------------- #
    async def record_message(
        self,
        *,
        self_id: int,
        scene: str,
        scene_id: int,
        user_id: int,
        content: str = "",
    ) -> None:
        await self.db.execute(
            "INSERT INTO message_log (self_id, scene, scene_id, user_id, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(self_id), scene, int(scene_id), int(user_id), content[:1000], time.time()),
        )
        await self.db.commit()

    async def daily_message_counts(self, days: int = 7) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT date(created_at, 'unixepoch', 'localtime') AS day, COUNT(*) AS count, "
            "COUNT(DISTINCT user_id) AS users "
            "FROM message_log WHERE date(created_at, 'unixepoch', 'localtime') >= "
            "date('now', 'localtime', ?) GROUP BY day ORDER BY day DESC LIMIT ?",
            (f"-{max(0, int(days) - 1)} days", int(days)),
        )
        rows = await cursor.fetchall()
        return [{"day": row["day"], "count": int(row["count"]), "users": int(row["users"])} for row in reversed(rows)]

    async def total_messages(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) AS count FROM message_log")
        row = await cursor.fetchone()
        return int(row["count"]) if row else 0

    async def top_users(self, limit: int = 10) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT user_id, COUNT(*) AS count FROM message_log GROUP BY user_id ORDER BY count DESC LIMIT ?",
            (int(limit),),
        )
        return [{"user_id": int(row["user_id"]), "count": int(row["count"])} for row in await cursor.fetchall()]

    # -- 提醒 ------------------------------------------------------------- #
    async def add_reminder(
        self,
        *,
        scene: str,
        scene_id: int,
        creator: int,
        content: str,
        due_at: float,
    ) -> int:
        cursor = await self.db.execute(
            "INSERT INTO reminders (scene, scene_id, creator, content, due_at, created_at, fired) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (scene, int(scene_id), int(creator), content, float(due_at), time.time()),
        )
        await self.db.commit()
        return int(cursor.lastrowid or 0)

    async def due_reminders(self, now: float | None = None, limit: int = 50) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT id, scene, scene_id, creator, content, due_at, created_at FROM reminders "
            "WHERE fired = 0 AND due_at <= ? ORDER BY due_at LIMIT ?",
            (float(now if now is not None else time.time()), int(limit)),
        )
        return [_reminder_row(row) for row in await cursor.fetchall()]

    async def mark_reminder_fired(self, reminder_id: int) -> None:
        await self.db.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (int(reminder_id),))
        await self.db.commit()

    async def list_reminders(
        self,
        scene: str,
        scene_id: int,
        *,
        include_fired: bool = False,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT id, scene, scene_id, creator, content, due_at, created_at FROM reminders "
            "WHERE scene = ? AND scene_id = ?"
        )
        if not include_fired:
            query += " AND fired = 0"
        query += " ORDER BY due_at"
        cursor = await self.db.execute(query, (scene, int(scene_id)))
        return [_reminder_row(row) for row in await cursor.fetchall()]

    async def delete_reminder(
        self,
        reminder_id: int,
        *,
        scene: str | None = None,
        scene_id: int | None = None,
    ) -> bool:
        query = "DELETE FROM reminders WHERE id = ?"
        params: list[Any] = [int(reminder_id)]
        if scene is not None and scene_id is not None:
            query += " AND scene = ? AND scene_id = ?"
            params.extend([scene, int(scene_id)])
        cursor = await self.db.execute(query, params)
        await self.db.commit()
        return bool(cursor.rowcount)

    async def count_reminders(self, scene: str, scene_id: int) -> int:
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS count FROM reminders WHERE scene = ? AND scene_id = ? AND fired = 0",
            (scene, int(scene_id)),
        )
        row = await cursor.fetchone()
        return int(row["count"]) if row else 0

    async def purge_fired_reminders(self, older_than_days: int = 7) -> int:
        cutoff = time.time() - max(1, int(older_than_days)) * 86400
        cursor = await self.db.execute(
            "DELETE FROM reminders WHERE fired = 1 AND due_at < ?", (cutoff,)
        )
        await self.db.commit()
        return int(cursor.rowcount or 0)

    # -- 维护 ------------------------------------------------------------- #
    async def summary(self) -> dict[str, Any]:
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "size_bytes": size,
            "schema_version": await self.schema_version(),
            "messages": await self.total_messages(),
        }

    async def vacuum(self) -> None:
        await self.db.execute("VACUUM")
        await self.db.commit()


def _reminder_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "scene": row["scene"],
        "scene_id": int(row["scene_id"]),
        "creator": int(row["creator"]),
        "content": row["content"],
        "due_at": float(row["due_at"]),
        "created_at": float(row["created_at"]),
    }


__all__ = ["ChatRecord", "MIGRATIONS", "SCHEMA_VERSION", "Storage"]
