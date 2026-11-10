"""Account/group-scoped raw memory and immutable, source-linked summaries."""

from __future__ import annotations

import json
from typing import Any


class GroupMemoryStore:
    def __init__(self, storage: Any) -> None:
        self.storage = storage

    async def append(
        self,
        self_id: int,
        group_id: int,
        role: str,
        content: str,
        *,
        created_at: float,
        user_id: int = 0,
        nickname: str = "",
        sender_role: str = "",
        source_id: int = 0,
        images: list[str] | None = None,
        generation: int | None = None,
        account_epoch: int | None = None,
        scope_epoch: int | None = None,
    ) -> int:
        async with self.storage.data_lock:
            try:
                if not self.storage.scope_current(self_id,'group',group_id,scope_epoch):
                    return 0
                if not self.storage.account_current(self_id, account_epoch):
                    return 0
                if generation is not None and generation != self.storage.conversation_generation:
                    return 0
                cursor = await self.storage.db.execute(
                    "INSERT OR IGNORE INTO group_memory_messages "
                    "(self_id,group_id,role,content,user_id,nickname,sender_role,source_id,created_at,images) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (self_id, group_id, role, content, user_id, nickname[:120], sender_role[:20], source_id, created_at, json.dumps(images or [])),
                )
                await self.storage.db.commit()
                if cursor.rowcount:
                    return int(cursor.lastrowid)
                cursor = await self.storage.db.execute(
                    "SELECT id FROM group_memory_messages WHERE self_id=? AND group_id=? AND source_id=? AND role='user'",
                    (self_id, group_id, source_id),
                )
                return int((await cursor.fetchone())[0])
            except BaseException:
                await self.storage.db.rollback()
                raise

    async def recent(self, self_id: int, group_id: int, before_id: int, limit: int = 2000) -> list[dict]:
        cursor = await self.storage.db.execute(
            "SELECT * FROM group_memory_messages WHERE self_id=? AND group_id=? AND id<? ORDER BY id DESC LIMIT ?",
            (self_id, group_id, before_id, limit),
        )
        return [dict(r) for r in reversed(await cursor.fetchall())]

    async def pending(self, self_id: int, group_id: int, before_id: int, privacy: str) -> list[dict]:
        cursor = await self.storage.db.execute(
            "SELECT * FROM group_memory_messages WHERE self_id=? AND group_id=? AND id<? "
            "AND id>COALESCE((SELECT MAX(last_id) FROM group_memory_summaries "
            "WHERE self_id=? AND group_id=? AND privacy=?),0) ORDER BY id LIMIT 80",
            (self_id, group_id, before_id, self_id, group_id, privacy),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def save_summary(
        self, self_id: int, group_id: int, rows: list[dict], privacy: str, topics: list[dict],
        *, generation: int | None = None, account_epoch: int | None = None, scope_epoch: int | None = None
    ) -> None:
        # Every source row must remain present; checking only the last row can resurrect deleted context.
        async with self.storage.data_lock:
            try:
                if not self.storage.scope_current(self_id,'group',group_id,scope_epoch):
                    return
                if not self.storage.account_current(self_id, account_epoch):
                    return
                if not rows or (generation is not None and generation != self.storage.conversation_generation):
                    return
                source_ids = list(dict.fromkeys(row['id'] for row in rows))
                placeholders = ','.join('?' for _ in source_ids)
                await self.storage.db.execute(
                    "INSERT OR IGNORE INTO group_memory_summaries "
                    "(self_id,group_id,first_id,last_id,started_at,ended_at,privacy,content) "
                    "SELECT ?,?,?,?,?,?,?,? WHERE "
                    f"(SELECT count(*) FROM group_memory_messages WHERE self_id=? AND group_id=? AND id IN ({placeholders}))=?",
                    (
                        self_id,
                        group_id,
                        rows[0]["id"],
                        rows[-1]["id"],
                        rows[0]["created_at"],
                        rows[-1]["created_at"],
                        privacy,
                        json.dumps(topics, ensure_ascii=False),
                        self_id,
                        group_id,
                        *source_ids,
                        len(source_ids),
                    ),
                )
                await self.storage.db.commit()
            except BaseException:
                await self.storage.db.rollback()
                raise

    async def summaries(self, self_id: int, group_id: int, privacy: str, since: float, before_id: int) -> list[dict]:
        cursor = await self.storage.db.execute(
            "SELECT * FROM group_memory_summaries WHERE self_id=? AND group_id=? AND privacy=? "
            "AND started_at>=? AND last_id<? ORDER BY last_id DESC LIMIT 6",
            (self_id, group_id, privacy, since, before_id),
        )
        return [dict(r) for r in reversed(await cursor.fetchall())]

    async def search(
        self,
        self_id: int,
        group_id: int,
        query: str,
        *,
        message_id: int = 0,
        start: float = 0,
        end: float = 1e20,
        before_id: int = 2**63 - 1,
    ) -> list[dict]:
        clauses = ["self_id=?", "group_id=?", "id<?", "created_at>=?", "created_at<?"]
        params: list[Any] = [self_id, group_id, before_id, start, end]
        if message_id:
            clauses.append("id=?")
            params.append(message_id)
        elif query.strip():
            terms = query.split()[:6]
            clauses.append("(" + " OR ".join("content LIKE ? ESCAPE '\\'" for _ in terms) + ")")
            params.extend(
                "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for term in terms
            )
        elif not start and end == 1e20:
            return []
        cursor = await self.storage.db.execute(
            "SELECT * FROM group_memory_messages WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT 8",
            params,
        )
        return [dict(r) for r in reversed(await cursor.fetchall())]

    async def next_message(self, self_id: int, group_id: int, row: dict, before_id: int, gap: int) -> int | None:
        cursor = await self.storage.db.execute(
            "SELECT id FROM group_memory_messages WHERE self_id=? AND group_id=? "
            "AND id>? AND id<? AND created_at<=? ORDER BY id LIMIT 1",
            (self_id, group_id, row["id"], before_id, row["created_at"] + gap),
        )
        following = await cursor.fetchone()
        return int(following["id"]) if following else None
