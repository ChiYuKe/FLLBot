"""Plugin: daily | Role: scoped persistent storage | Metadata: plugin.toml."""

import sqlite3
from contextlib import contextmanager


@contextmanager
def database(directory):
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "daily.sqlite3")
    db.row_factory = sqlite3.Row
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, bot INTEGER NOT NULL, scene INTEGER NOT NULL,
                owner INTEGER NOT NULL, content TEXT NOT NULL, due REAL,
                state TEXT NOT NULL DEFAULT 'pending', notified INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS task_due ON tasks(state, notified, due);
            CREATE TABLE IF NOT EXISTS knowledge (
                bot INTEGER NOT NULL, scene INTEGER NOT NULL, title TEXT NOT NULL,
                content TEXT NOT NULL, editor INTEGER NOT NULL,
                PRIMARY KEY(bot, scene, title));
        """)
        if db.execute('PRAGMA user_version').fetchone()[0] < 1:
            db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE tasks ADD COLUMN delivery TEXT NOT NULL DEFAULT 'waiting';
                ALTER TABLE tasks ADD COLUMN last_code TEXT NOT NULL DEFAULT '';
                ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
                UPDATE tasks SET delivery='sent' WHERE notified=1;
                CREATE TABLE task_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
                    status TEXT NOT NULL, code TEXT NOT NULL, created_at REAL NOT NULL,
                    finished_at REAL);
                CREATE INDEX task_runs_task ON task_runs(task_id,id DESC);
                PRAGMA user_version=1;
                COMMIT;
            """)
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def scope(ctx):
    if not ctx.self_id or not ctx.user_id or (ctx.is_group and not ctx.group_id):
        raise ValueError("无法确定机器人或当前会话")
    return ctx.self_id, int(ctx.group_id) if ctx.is_group else -ctx.user_id
