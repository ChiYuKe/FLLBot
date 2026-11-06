"""Snapshot previews and atomic cleanup on a dedicated SQLite connection."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import aiosqlite

MAX_ROWS = 50000


class CleanupChangedError(RuntimeError):
    pass


def queries(spec: dict[str, Any], batch: int | None = None) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    params = {'scene': spec.get('scene'), 'scene_id': spec.get('scene_id'), 'self_id': spec.get('self_id')}
    params.update({f'cut_{key}': value for key, value in spec['cutoffs'].items()})

    def scope(table: str) -> str:
        where = []
        grouped = table in {'group_memory_messages', 'group_memory_summaries', 'management_changes'}
        if table == 'management_changes' and spec.get('scene') == 'private':
            # Global and group audit entries have no private-conversation owner.
            # A private-session preview must never erase unrelated global audit history.
            return '0=1'
        if grouped and spec.get('scene') == 'private':
            return '0'
        if not grouped and spec.get('scene'):
            where.append('scene=:scene')
        if spec.get('scene_id') is not None:
            where.append(('group_id' if grouped else 'scene_id')+'=:scene_id')
        if spec.get('self_id') is not None:
            where.append('self_id=:self_id')
        return ' AND '.join(where) or '1'

    def limited(table: str, where: str) -> str:
        identifier = 'rowid' if table == 'trigger_decisions' else 'id'
        return f'{identifier} IN (SELECT {identifier} FROM {table} WHERE {where} ORDER BY {identifier} LIMIT {int(batch)})' if batch else where

    result = []
    for category, table, clock in [('messages', 'message_log', 'created_at'), ('usage', 'llm_usage', 'created_at'),
                                   ('traces', 'message_traces', 'started_at'), ('traces','trigger_decisions','created_at'), ('traces','message_events','received_at'), ('changes', 'management_changes', 'created_at')]:
        if category in spec['cutoffs']:
            where = scope(table)+f' AND {clock}<:cut_{category}'
            result.append((table, limited(table, where)))
    if 'conversations' in spec['cutoffs']:
        raw = limited('group_memory_messages', scope('group_memory_messages')+' AND created_at<:cut_conversations')
        old_summaries = limited('group_memory_summaries', scope('group_memory_summaries')+' AND ended_at<:cut_conversations')
        linked = ('EXISTS (SELECT 1 FROM group_memory_messages AS r WHERE '
                  'r.self_id=group_memory_summaries.self_id AND r.group_id=group_memory_summaries.group_id '
                  'AND r.id BETWEEN group_memory_summaries.first_id AND group_memory_summaries.last_id '
                  f'AND r.id IN (SELECT id FROM group_memory_messages WHERE {raw}))')
        result.append(('turn_messages', limited('turn_messages',scope('turn_messages')+' AND created_at<:cut_conversations')))
        result.append(('tool_results', 'execution_id IN (SELECT execution_id FROM tool_executions WHERE '+scope('tool_executions')+' AND created_at<:cut_conversations)'))
        deliveries = scope('reply_deliveries')+' AND bodies_cleared=0 AND created_at<:cut_conversations'
        if batch:
            deliveries = f'rowid IN (SELECT rowid FROM reply_deliveries WHERE {deliveries} ORDER BY rowid LIMIT {int(batch)})'
        result += [('delivery_parts', 'delivery_id IN (SELECT delivery_id FROM reply_deliveries WHERE '+deliveries+')'),
                   ('reply_deliveries', deliveries)]
        # Delete summaries first while source rows still exist for the linkage predicate.
        result += [('group_memory_summaries', f'({old_summaries}) OR ({scope("group_memory_summaries")} AND {linked})'),
                   ('group_memory_messages', raw),
                   ('chat_messages', limited('chat_messages', scope('chat_messages')+' AND created_at<:cut_conversations'))]
    if 'traces' in spec['cutoffs']:
        where = 'turn_id IN (SELECT turn_id FROM turns WHERE '+scope('turns')+') AND created_at<:cut_traces'
        result.append(('agent_progress', limited('agent_progress', where)))
    return result, params


async def plan(connection: aiosqlite.Connection, spec: dict[str, Any], batch: int | None = None) -> dict[str, Any]:
    statements, params = queries(spec, batch)
    counts = {}
    for table, where in statements:
        row = await (await connection.execute(f'SELECT count(*) FROM {table} WHERE {where}', params)).fetchone()
        counts[table] = int(row[0])
    total = sum(counts.values())
    signature = hashlib.sha256()
    if total <= MAX_ROWS:
        for table, where in statements:
            signature.update(table.encode())
            identifier = 'rowid' if table in {'reply_deliveries', 'delivery_parts', 'tool_results', 'trigger_decisions'} else 'id'
            cursor = await connection.execute(f'SELECT {identifier} FROM {table} WHERE {where} ORDER BY {identifier}', params)
            while rows := await cursor.fetchmany(512):
                signature.update(','.join(str(row[0]) for row in rows).encode()+b';')
    return {'counts': counts, 'total': total, 'signature': signature.hexdigest(), 'can_execute': 0 < total <= MAX_ROWS,
            'max_rows': MAX_ROWS}


async def preview(storage: Any, spec: dict[str, Any]) -> dict[str, Any]:
    async with aiosqlite.connect(storage.path) as connection:
        await connection.execute('BEGIN')
        return await plan(connection, spec)


async def execute(storage: Any, spec: dict[str, Any], *, expected: str | None = None, kind: str = 'manual',
                  batch: int | None = None) -> dict[str, Any]:
    async with storage.data_lock:
        async with aiosqlite.connect(storage.path, timeout=5) as connection:
            try:
                await connection.execute('BEGIN IMMEDIATE')
                current = await plan(connection, spec, batch)
                if expected is not None and current['signature'] != expected:
                    raise CleanupChangedError('待清理记录已变化，请重新预览；本次未删除数据')
                if current['total'] > MAX_ROWS:
                    raise CleanupChangedError('本次记录超过 50000 条，请缩小时间或会话范围后重新预览')
                statements, params = queries(spec, batch)
                for table, where in statements:
                    if table == 'delivery_parts':
                        await connection.execute("UPDATE delivery_parts SET content='',status=CASE WHEN status='pending' "
                                                 "THEN 'cancelled' ELSE status END WHERE "+where,params)
                    elif table == 'reply_deliveries':
                        await connection.execute("UPDATE reply_deliveries SET candidate='',metadata='{}',bodies_cleared=1 WHERE "+where,params)
                    else:
                        await connection.execute(f'DELETE FROM {table} WHERE {where}', params)
                run_id = None
                if current['total']:
                    cursor = await connection.execute(
                        'INSERT INTO cleanup_runs(kind,created_at,specification,counts) VALUES(?,?,?,?)',
                        (kind, time.time(), json.dumps(spec), json.dumps(current['counts'])),
                    )
                    run_id = cursor.lastrowid
                    await connection.execute('DELETE FROM cleanup_runs WHERE id <= (SELECT max(id)-1000 FROM cleanup_runs)')
                change = None
                if any(current['counts'].get(table, 0) for table in ('chat_messages', 'group_memory_messages', 'group_memory_summaries', 'reply_deliveries', 'delivery_parts', 'tool_results', 'turn_messages')):
                    change = await storage.advance_scope_epoch(connection,spec.get('self_id'),spec.get('scene'),spec.get('scene_id'))
                await storage.commit_scope_epoch(connection,change)
                return {'run_id': run_id, 'counts': current['counts'], 'total': current['total']}
            except BaseException:
                await connection.rollback()
                raise
