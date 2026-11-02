"""Atomic resource binding replacement; no file or credential mutations."""

import json
import time

import aiosqlite


async def snapshot(connection):
    groups = await (await connection.execute('SELECT group_id,enabled,config FROM groups ORDER BY group_id')).fetchall()
    profiles = await (await connection.execute("SELECT key,value FROM kv WHERE scope='bot-profiles' ORDER BY key")).fetchall()
    overrides = await (await connection.execute("SELECT value FROM meta WHERE key='config_overrides'")).fetchone()
    return {'groups': [{'group_id': row[0], 'enabled': bool(row[1]), 'config': json.loads(row[2])} for row in groups],
            'profiles': {row[0]: json.loads(row[1]) for row in profiles},
            'overrides': json.loads(overrides[0]) if overrides else {}}


async def read(path):
    async with aiosqlite.connect(path) as connection:
        await connection.execute('BEGIN')
        return await snapshot(connection)


async def replace(path, expected, candidate, *, kind, source, target, references):
    """Recheck and write every binding and its audit entry in one transaction."""
    async with aiosqlite.connect(path, timeout=5) as connection:
        try:
            await connection.execute('BEGIN IMMEDIATE')
            if await snapshot(connection) != expected:
                raise ValueError('数据库配置已变化，请重新预览；本次未修改引用')
            now = time.time()
            if candidate['overrides'] != expected['overrides']:
                await connection.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                         ('config_overrides', json.dumps(candidate['overrides'], ensure_ascii=False)))
            for key, profile in candidate['profiles'].items():
                if profile != expected['profiles'][key]:
                    await connection.execute("UPDATE kv SET value=?,updated_at=? WHERE scope='bot-profiles' AND key=?",
                                             (json.dumps(profile, ensure_ascii=False), now, key))
            for group, previous in zip(candidate['groups'], expected['groups'], strict=True):
                if group != previous:
                    await connection.execute('UPDATE groups SET config=?,updated_at=? WHERE group_id=?',
                                             (json.dumps(group['config'], ensure_ascii=False), now, group['group_id']))
            fields = [f'{kind}:{source}->{target}'] + [f"{item['scope']}:{item['object_id']}:{item['field']}" for item in references]
            cursor = await connection.execute('INSERT INTO management_changes(group_id,action,fields,created_at) VALUES(0,?,?,?)',
                                              ('resource_replaced', json.dumps(fields, ensure_ascii=False), now))
            await connection.commit()
            return cursor.lastrowid
        except BaseException:
            await connection.rollback()
            raise
