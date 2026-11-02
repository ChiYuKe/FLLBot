"""Plugin-owned task views and transitions; delivery content stays in tasks only."""

import time


def record(db, task_id, status, code):
    now = time.time()
    cursor = db.execute(
        'INSERT INTO task_runs(task_id,status,code,created_at,finished_at) VALUES(?,?,?,?,?)',
        (task_id, status, code, now, None if status == 'sending' else now),
    )
    db.execute('DELETE FROM task_runs WHERE id <= (SELECT max(id)-10000 FROM task_runs)')
    return cursor.lastrowid


def recover(db):
    """A missing delivery receipt after interruption never authorizes a resend."""
    for row in db.execute("SELECT id FROM tasks WHERE delivery='sending'").fetchall():
        db.execute("UPDATE tasks SET delivery='uncertain',last_code='interrupted',version=version+1 WHERE id=?", (row['id'],))
        db.execute("UPDATE task_runs SET status='uncertain',code='interrupted',finished_at=? WHERE task_id=? AND status='sending'",
                   (time.time(), row['id']))


def view(row):
    item = dict(row)
    item['revision'] = str(item.pop('version'))
    item['self_id'] = item.pop('bot')
    signed_scene = item['scene']
    item['scene'] = 'group' if signed_scene > 0 else 'private'
    item['scene_id'] = abs(signed_scene)
    item['due_at'] = item.pop('due')
    item['timezone'] = 'UTC+08:00'
    actions = []
    if item['state'] in {'pending', 'paused'} and item['delivery'] != 'sending':
        actions += ['cancel', 'complete']
        if item['state'] == 'paused':
            actions += ['resume']
        elif item['due_at'] is not None and item['delivery'] != 'sent':
            actions += ['pause']
            if item['delivery'] == 'uncertain':
                actions += ['retry']
    item['actions'] = actions
    return item


def listing(db, *, before=None, limit=20, self_id=None, scene=None, scene_id=None, owner=None, state=None):
    clauses, params = ['1'], []
    for column, value in [('bot', self_id), ('owner', owner), ('state', state)]:
        if value is not None:
            clauses.append(f'{column}=?')
            params.append(value)
    if before is not None:
        clauses.append('id<?')
        params.append(before)
    if scene:
        clauses.append('scene>0' if scene == 'group' else 'scene<0')
    if scene_id is not None:
        clauses.append('scene=?')
        params.append(scene_id if scene == 'group' else -scene_id)
    rows = db.execute('SELECT * FROM tasks WHERE '+' AND '.join(clauses)+' ORDER BY id DESC LIMIT ?', [*params, limit+1]).fetchall()
    items = [view(row) for row in rows[:limit]]
    return {'items': items, 'next_cursor': items[-1]['id'] if len(rows)>limit else None}


def get(db, task_id):
    row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    return row


def runs(db, task_id, *, before=None, limit=20):
    get(db, task_id)
    rows = db.execute('SELECT * FROM task_runs WHERE task_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?',
                      (task_id, before, before, limit+1)).fetchall()
    items = [dict(row) for row in rows[:limit]]
    return {'items': items, 'next_cursor': items[-1]['id'] if len(rows)>limit else None}


def action(db, task_id, action, revision, acknowledge_duplicate=False):
    row = get(db, task_id)
    item = view(row)
    if revision != item['revision']:
        raise ValueError('任务已变化，请刷新列表后重新核对')
    if action not in item['actions']:
        raise ValueError('当前状态不支持此操作；发送中的任务需等待回执')
    if action == 'retry' and not acknowledge_duplicate:
        raise ValueError('上次送达结果不确定，重试可能重复发送；必须明确确认')
    if action == 'retry':
        db.execute("UPDATE tasks SET delivery='waiting',last_code='retry_requested',retry_at=0,version=version+1 WHERE id=?", (task_id,))
    else:
        state = {'pause': 'paused', 'resume': 'pending', 'cancel': 'cancelled', 'complete': 'completed'}[action]
        db.execute('UPDATE tasks SET state=?,version=version+1 WHERE id=?', (state, task_id))
    record(db, task_id, 'action', action)
    return view(get(db, task_id))
