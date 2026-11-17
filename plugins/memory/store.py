"""Plugin: memory | Role: scoped SQLite storage | Metadata: plugin.toml.

A fact is only visible inside the exact scope it was written to::

    (self_id, scene, scene_id, user_id)

``scene`` is ``group`` or ``private``; ``scene_id`` is the group number or the
private chat's user number. Every query takes the whole scope, so a fact can
not leak across groups, private chats, bot accounts or users. The plugin owns
this small SQLite file under ``data/plugins/memory/`` and never writes to the
host database.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATABASE_NAME = "memory.sqlite3"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Scope:
    """一次会话中唯一可见的记忆范围：机器人账号 + 场景 + 会话 + 用户。"""

    self_id: int
    scene: str
    scene_id: int
    user_id: int

    def params(self) -> tuple[int, str, int, int]:
        return (self.self_id, self.scene, self.scene_id, self.user_id)


def scope_of(ctx: Any) -> Scope:
    """从当前消息上下文取得隔离键；缺少账号或用户时拒绝访问。"""
    self_id = int(getattr(ctx, "self_id", 0) or 0)
    user_id = int(getattr(ctx, "user_id", 0) or 0)
    if self_id <= 0 or user_id <= 0:
        raise ValueError("无法确定机器人账号或当前用户")
    if getattr(ctx, "is_group", False):
        group_id = int(getattr(ctx, "group_id", 0) or 0)
        if group_id <= 0:
            raise ValueError("无法确定当前群")
        return Scope(self_id, "group", group_id, user_id)
    if not getattr(ctx, "is_private", False):
        raise ValueError("只能处理群聊或私聊消息")
    return Scope(self_id, "private", user_id, user_id)


@contextmanager
def database(directory: Path) -> Iterator[sqlite3.Connection]:
    """打开插件数据库；建表与迁移只依赖 PRAGMA user_version。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / DATABASE_NAME, timeout=5.0)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        migrate(db)
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def migrate(db: sqlite3.Connection) -> None:
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version < 1:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                self_id INTEGER NOT NULL,
                scene TEXT NOT NULL CHECK(scene IN ('group','private')),
                scene_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_key TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'fact',
                tags TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS facts_scope
                ON facts(self_id, scene, scene_id, user_id, id DESC);
            CREATE INDEX IF NOT EXISTS facts_expiry ON facts(expires_at);
            PRAGMA user_version=1;
            """
        )


def duplicate(db: sqlite3.Connection, scope: Scope, content_key: str, now: float) -> sqlite3.Row | None:
    """同一范围内内容相同的有效记忆；用于去重而不是重复占用额度。"""
    return db.execute(
        "SELECT * FROM facts WHERE self_id=? AND scene=? AND scene_id=? AND user_id=? "
        "AND content_key=? AND expires_at>? ORDER BY id DESC LIMIT 1",
        (*scope.params(), content_key, float(now)),
    ).fetchone()


def touch(db: sqlite3.Connection, fact_id: int, scope: Scope, now: float, expires_at: float) -> None:
    """重新确认已有记忆：刷新顺序与过期时间，内容保持不变。"""
    db.execute(
        "UPDATE facts SET created_at=?, expires_at=? "
        "WHERE id=? AND self_id=? AND scene=? AND scene_id=? AND user_id=?",
        (float(now), float(expires_at), int(fact_id), *scope.params()),
    )


def count(db: sqlite3.Connection, scope: Scope, now: float) -> int:
    row = db.execute(
        "SELECT COUNT(*) FROM facts WHERE self_id=? AND scene=? AND scene_id=? AND user_id=? AND expires_at>?",
        (*scope.params(), float(now)),
    ).fetchone()
    return int(row[0]) if row else 0


def insert(
    db: sqlite3.Connection,
    scope: Scope,
    *,
    content: str,
    content_key: str,
    kind: str,
    tags: list[str],
    created_at: float,
    expires_at: float,
) -> int:
    cursor = db.execute(
        "INSERT INTO facts(self_id,scene,scene_id,user_id,content,content_key,kind,tags,created_at,expires_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            *scope.params(),
            content,
            content_key,
            kind,
            ",".join(tags),
            float(created_at),
            float(expires_at),
        ),
    )
    return int(cursor.lastrowid)


def recent(db: sqlite3.Connection, scope: Scope, limit: int, now: float) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM facts WHERE self_id=? AND scene=? AND scene_id=? AND user_id=? AND expires_at>? "
        "ORDER BY id DESC LIMIT ?",
        (*scope.params(), float(now), int(limit)),
    ).fetchall()


def search(db: sqlite3.Connection, scope: Scope, query: str, limit: int, now: float) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM facts WHERE self_id=? AND scene=? AND scene_id=? AND user_id=? AND expires_at>? "
        "AND (instr(lower(content), lower(?))>0 OR instr(lower(tags), lower(?))>0) "
        "ORDER BY id DESC LIMIT ?",
        (*scope.params(), float(now), query, query, int(limit)),
    ).fetchall()


def delete(db: sqlite3.Connection, scope: Scope, fact_id: int) -> int:
    """只删除当前范围自己的那条记忆；别人的编号不会命中。"""
    cursor = db.execute(
        "DELETE FROM facts WHERE id=? AND self_id=? AND scene=? AND scene_id=? AND user_id=?",
        (int(fact_id), *scope.params()),
    )
    return int(cursor.rowcount)


def delete_all(db: sqlite3.Connection, scope: Scope) -> int:
    cursor = db.execute(
        "DELETE FROM facts WHERE self_id=? AND scene=? AND scene_id=? AND user_id=?",
        scope.params(),
    )
    return int(cursor.rowcount)


def delete_expired(db: sqlite3.Connection, now: float) -> int:
    cursor = db.execute("DELETE FROM facts WHERE expires_at<=?", (float(now),))
    return int(cursor.rowcount)
