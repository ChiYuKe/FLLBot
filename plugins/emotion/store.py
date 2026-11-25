# Plugin: emotion | Role: SQLite persistence | Metadata: plugin.toml
"""Durable mood, whisper log and per-user opt-outs.

The plugin owns this small SQLite file under ``data/plugins/emotion/`` so host
chat/history retention never has to understand emotion internals.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .mood import Mood

RETENTION_SECONDS = 30 * 24 * 3600
INTERACTION_RETENTION_SECONDS = 180 * 24 * 3600


class MoodStore:
    """Synchronous SQLite store; callers run on the event-loop thread."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db: sqlite3.Connection | None = sqlite3.connect(
            self.path, timeout=5.0, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._create_schema()
        self.prune()

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("情绪数据库已关闭")
        return self._db

    def _create_schema(self) -> None:
        db = self._connection()
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS moods("
                "self_id INTEGER NOT NULL, group_id INTEGER NOT NULL, "
                "valence REAL NOT NULL, energy REAL NOT NULL, "
                "updated_at REAL NOT NULL, last_reason TEXT NOT NULL DEFAULT '', "
                "last_event_at REAL NOT NULL DEFAULT 0, "
                "PRIMARY KEY(self_id, group_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS whispers("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, self_id INTEGER NOT NULL, "
                "group_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                "nickname TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, "
                "content TEXT NOT NULL, created_at REAL NOT NULL, "
                "status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', "
                "delivery TEXT NOT NULL DEFAULT '')"
            )
            if "delivery" not in {row["name"] for row in db.execute("PRAGMA table_info(whispers)")}:
                db.execute("ALTER TABLE whispers ADD COLUMN delivery TEXT NOT NULL DEFAULT ''")
            db.execute(
                "CREATE INDEX IF NOT EXISTS whispers_group ON whispers(self_id, group_id, created_at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS whispers_user ON whispers(self_id, user_id, created_at)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS optouts("
                "self_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                "created_at REAL NOT NULL, PRIMARY KEY(self_id, user_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS interactions("
                "self_id INTEGER NOT NULL, group_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                "last_at REAL NOT NULL, PRIMARY KEY(self_id, group_id, user_id))"
            )

    # -- mood ------------------------------------------------------------- #
    def load_moods(self) -> dict[tuple[int, int], Mood]:
        rows = self._connection().execute(
            "SELECT self_id, group_id, valence, energy, updated_at, last_reason, last_event_at FROM moods"
        ).fetchall()
        result: dict[tuple[int, int], Mood] = {}
        for row in rows:
            mood = Mood(
                self_id=int(row["self_id"]),
                group_id=int(row["group_id"]),
                valence=float(row["valence"]),
                energy=float(row["energy"]),
                updated_at=float(row["updated_at"]),
                last_reason=str(row["last_reason"]),
                last_event_at=float(row["last_event_at"]),
            )
            result[(mood.self_id, mood.group_id)] = mood
        return result

    def save_moods(self, moods: list[Mood]) -> None:
        if not moods:
            return
        db = self._connection()
        with db:
            db.executemany(
                "INSERT INTO moods(self_id, group_id, valence, energy, updated_at, last_reason, last_event_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(self_id, group_id) DO UPDATE SET "
                "valence=excluded.valence, energy=excluded.energy, updated_at=excluded.updated_at, "
                "last_reason=excluded.last_reason, last_event_at=excluded.last_event_at",
                [
                    (
                        mood.self_id,
                        mood.group_id,
                        mood.valence,
                        mood.energy,
                        mood.updated_at,
                        mood.last_reason,
                        mood.last_event_at,
                    )
                    for mood in moods
                ],
            )

    # -- whisper log ------------------------------------------------------ #
    def record_whisper(
        self,
        *,
        self_id: int,
        group_id: int,
        user_id: int,
        nickname: str,
        kind: str,
        content: str,
        created_at: float,
        status: str,
        error: str = "",
        delivery: str = "",
    ) -> None:
        db = self._connection()
        with db:
            db.execute(
                "INSERT INTO whispers(self_id, group_id, user_id, nickname, kind, content, created_at, status, error, delivery) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    int(self_id),
                    int(group_id),
                    int(user_id),
                    str(nickname or "")[:80],
                    str(kind or "secret")[:20],
                    str(content or "")[:500],
                    float(created_at),
                    str(status or "sent")[:20],
                    str(error or "")[:120],
                    delivery,
                ),
            )

    def count_since(self, self_id: int, since: float, *, group_id: int | None = None) -> int:
        db = self._connection()
        if group_id is None:
            row = db.execute(
                "SELECT COUNT(*) FROM whispers WHERE self_id=? AND created_at>=? AND status='sent'",
                (int(self_id), float(since)),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) FROM whispers WHERE self_id=? AND group_id=? AND created_at>=? AND status='sent'",
                (int(self_id), int(group_id), float(since)),
            ).fetchone()
        return int(row[0]) if row else 0

    def last_group_at(self, self_id: int, group_id: int, *, sent_only: bool = False) -> float:
        status_clause = " AND status='sent'" if sent_only else ""
        row = self._connection().execute(
            "SELECT MAX(created_at) FROM whispers WHERE self_id=? AND group_id=?" + status_clause,
            (int(self_id), int(group_id)),
        ).fetchone()
        return float(row[0] or 0.0) if row else 0.0

    def last_user_at(self, self_id: int, user_id: int, *, sent_only: bool = False) -> float:
        status_clause = " AND status='sent'" if sent_only else ""
        row = self._connection().execute(
            "SELECT MAX(created_at) FROM whispers WHERE self_id=? AND user_id=?" + status_clause,
            (int(self_id), int(user_id)),
        ).fetchone()
        return float(row[0] or 0.0) if row else 0.0

    def last_group_target(self, self_id: int, group_id: int) -> int:
        row = self._connection().execute(
            "SELECT user_id FROM whispers WHERE self_id=? AND group_id=? AND status='sent' "
            "ORDER BY id DESC LIMIT 1",
            (int(self_id), int(group_id)),
        ).fetchone()
        return int(row["user_id"]) if row else 0

    def recent_group_contents(self, self_id: int, group_id: int, limit: int = 6) -> list[str]:
        rows = self._connection().execute(
            "SELECT content FROM whispers WHERE self_id=? AND group_id=? AND status='sent' "
            "ORDER BY id DESC LIMIT ?",
            (int(self_id), int(group_id), max(1, int(limit))),
        ).fetchall()
        return [str(row["content"]) for row in rows]

    def recent_sent_targets(self, since: float) -> dict[tuple[int, int], dict[tuple[int, str], float]]:
        """按 Bot、用户及投递来源恢复最近的互聊窗口。"""
        rows = self._connection().execute(
            "SELECT self_id, user_id, group_id, delivery, MAX(created_at) AS last_at FROM whispers "
            "WHERE status='sent' AND created_at>=? GROUP BY self_id, user_id, group_id, delivery",
            (float(since),),
        )
        result: dict[tuple[int, int], dict[tuple[int, str], float]] = {}
        for row in rows:
            sessions = result.setdefault((row["self_id"], row["user_id"]), {})
            sessions[(row["group_id"], row["delivery"])] = row["last_at"]
        return result

    # -- opt-outs --------------------------------------------------------- #
    def load_optouts(self) -> set[tuple[int, int]]:
        rows = self._connection().execute("SELECT self_id, user_id FROM optouts").fetchall()
        return {(int(row["self_id"]), int(row["user_id"])) for row in rows}

    def set_optout(self, self_id: int, user_id: int, opted_out: bool) -> None:
        db = self._connection()
        with db:
            if opted_out:
                db.execute(
                    "INSERT OR REPLACE INTO optouts(self_id, user_id, created_at) VALUES(?,?,?)",
                    (int(self_id), int(user_id), time.time()),
                )
            else:
                db.execute(
                    "DELETE FROM optouts WHERE self_id=? AND user_id=?",
                    (int(self_id), int(user_id)),
                )

    # -- interactions ----------------------------------------------------- #
    def record_interaction(self, self_id: int, group_id: int, user_id: int, at: float) -> None:
        """Remember that a user talked to the bot; group 0 means a private chat."""
        if int(user_id) <= 0:
            return
        db = self._connection()
        with db:
            db.execute(
                "INSERT INTO interactions(self_id, group_id, user_id, last_at) VALUES(?,?,?,?) "
                "ON CONFLICT(self_id, group_id, user_id) DO UPDATE SET "
                "last_at=MAX(interactions.last_at, excluded.last_at)",
                (int(self_id), int(group_id), int(user_id), float(at)),
            )

    def interacted_users(self, self_id: int, group_id: int, since: float) -> set[int]:
        """Users who talked to the bot in this group or in private since ``since``."""
        rows = self._connection().execute(
            "SELECT user_id FROM interactions WHERE self_id=? AND last_at>=? AND (group_id=? OR group_id=0)",
            (int(self_id), float(since), int(group_id)),
        ).fetchall()
        return {int(row["user_id"]) for row in rows}

    # -- maintenance ------------------------------------------------------ #
    def prune(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        db = self._connection()
        with db:
            db.execute("DELETE FROM whispers WHERE created_at<?", (cutoff,))
            db.execute(
                "DELETE FROM interactions WHERE last_at<?",
                (time.time() - INTERACTION_RETENTION_SECONDS,),
            )

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None


__all__ = ["MoodStore"]
