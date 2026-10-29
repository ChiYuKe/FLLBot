"""SQLite 持久化：迁移、键值、群配置、插件状态、聊天历史与统计。"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, list[str]] = {
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
    ]
}


class Storage:
    """单连接 SQLite 封装。所有写入走同一连接，天然串行。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.logger = logging.getLogger("fllbot.storage")
        self._db: aiosqlite.Connection | None = None

    # -- 生命周期 --------------------------------------------------------- #
    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._migrate()
        self.logger.info("数据库就绪：%s", self.path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

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

    async def kv_delete(self, scope: str, key: str) -> None:
        await self.db.execute("DELETE FROM kv WHERE scope = ? AND key = ?", (scope, key))
        await self.db.commit()

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

    # -- 群 --------------------------------------------------------------- #
    async def upsert_group(
        self,
        group_id: int,
        *,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        cursor = await self.db.execute("SELECT group_id, enabled, config FROM groups WHERE group_id = ?", (group_id,))
        row = await cursor.fetchone()
        if row is None:
            await self.db.execute(
                "INSERT INTO groups (group_id, enabled, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    group_id,
                    int(bool(enabled)) if enabled is not None else 1,
                    json.dumps(config or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        else:
            new_enabled = row["enabled"] if enabled is None else int(bool(enabled))
            new_config = row["config"] if config is None else json.dumps(config, ensure_ascii=False)
            await self.db.execute(
                "UPDATE groups SET enabled = ?, config = ?, updated_at = ? WHERE group_id = ?",
                (new_enabled, new_config, now, group_id),
            )
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

    async def delete_group(self, group_id: int) -> None:
        await self.db.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
        await self.db.commit()

    # -- 插件状态 --------------------------------------------------------- #
    async def set_plugin_state(self, plugin: str, group_id: int, enabled: bool) -> None:
        await self.db.execute(
            "INSERT INTO plugin_state (plugin, group_id, enabled, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(plugin, group_id) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
            (plugin, int(group_id), int(bool(enabled)), time.time()),
        )
        await self.db.commit()

    async def list_plugin_states(self) -> dict[tuple[str, int], bool]:
        cursor = await self.db.execute("SELECT plugin, group_id, enabled FROM plugin_state")
        return {(row["plugin"], int(row["group_id"])): bool(row["enabled"]) for row in await cursor.fetchall()}

    async def clear_plugin_states(self, plugin: str) -> None:
        await self.db.execute("DELETE FROM plugin_state WHERE plugin = ?", (plugin,))
        await self.db.commit()

    # -- 聊天历史 --------------------------------------------------------- #
    async def add_chat_message(self, scene: str, scene_id: int, role: str, content: str) -> None:
        await self.db.execute(
            "INSERT INTO chat_messages (scene, scene_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (scene, int(scene_id), role, content, time.time()),
        )
        await self.db.commit()

    async def recent_chat(self, scene: str, scene_id: int, limit: int = 20) -> list[tuple[str, str]]:
        cursor = await self.db.execute(
            "SELECT role, content FROM chat_messages WHERE scene = ? AND scene_id = ? ORDER BY id DESC LIMIT ?",
            (scene, int(scene_id), int(limit)),
        )
        rows = await cursor.fetchall()
        return [(row["role"], row["content"]) for row in reversed(rows)]

    async def clear_chat(self, scene: str, scene_id: int) -> int:
        cursor = await self.db.execute(
            "DELETE FROM chat_messages WHERE scene = ? AND scene_id = ?", (scene, int(scene_id))
        )
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
            "FROM message_log GROUP BY day ORDER BY day DESC LIMIT ?",
            (int(days),),
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


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "Storage"]
