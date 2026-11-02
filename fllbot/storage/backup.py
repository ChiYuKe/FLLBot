"""Consistent SQLite backups, verified restores, and a cross-process DB lock."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path


class DatabaseInUseError(RuntimeError):
    """The configured database is currently held by a running FLLBot process."""


class DatabaseFileLock:
    """Advisory process lock kept for the lifetime of an open Storage connection."""

    def __init__(self, database: Path | str) -> None:
        self.path = Path(f"{Path(database).resolve()}.lock")
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise DatabaseInUseError(f"数据库正在使用：{self.path.with_suffix('')}") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise DatabaseInUseError(f"数据库正在使用：{self.path.with_suffix('')}") from exc
            self._file = handle
        except BaseException:
            handle.close()
            raise

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> DatabaseFileLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def validate_database(path: Path | str, *, max_schema_version: int) -> dict[str, int]:
    """Require a readable SQLite database, a clean integrity check, and a supported schema."""
    database = Path(path).resolve()
    if not database.is_file():
        raise ValueError(f"数据库文件不存在：{database}")
    uri = f"{database.as_uri()}?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            detail = str(check[0])[:300] if check else "无返回结果"
            raise ValueError(f"SQLite 完整性检查失败：{detail}")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < 1 or version > max_schema_version:
            raise ValueError(f"数据库版本不受支持：{version}（当前最高版本 {max_schema_version}）")
        table_count = int(connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0])
        if table_count == 0:
            raise ValueError("数据库没有应用数据表")
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"无法读取 SQLite 数据库：{exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    return {"schema_version": version, "tables": table_count, "size_bytes": database.stat().st_size}


def _copy_database(source: Path, destination: Path, *, max_schema_version: int) -> dict[str, int]:
    if source.resolve() == destination.resolve():
        raise ValueError("源数据库与目标路径不能相同")
    if not source.is_file():
        raise ValueError(f"数据库文件不存在：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        current: sqlite3.Connection | None = None
        copied: sqlite3.Connection | None = None
        try:
            current = sqlite3.connect(source, timeout=5)
            copied = sqlite3.connect(temp, timeout=5)
        except sqlite3.DatabaseError as exc:
            if copied is not None:
                copied.close()
            if current is not None:
                current.close()
            raise ValueError(f"无法打开 SQLite 数据库：{exc}") from exc
        try:
            assert current is not None and copied is not None
            current.backup(copied, pages=256, sleep=0.05)
            copied.commit()
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"SQLite 备份失败：{exc}") from exc
        finally:
            if copied is not None:
                copied.close()
            if current is not None:
                current.close()
        metadata = validate_database(temp, max_schema_version=max_schema_version)
        os.replace(temp, destination)
        metadata["size_bytes"] = destination.stat().st_size
        return metadata
    finally:
        temp.unlink(missing_ok=True)


def create_backup(source: Path | str, destination: Path | str, *, max_schema_version: int) -> dict[str, int]:
    """Create a transactionally consistent SQLite backup and publish it atomically."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    metadata = _copy_database(source_path, destination_path, max_schema_version=max_schema_version)
    return {**metadata, "created_at": int(time.time())}


def restore_backup(
    source: Path | str,
    destination: Path | str,
    *,
    max_schema_version: int,
) -> tuple[dict[str, int], Path | None]:
    """Verify a backup, lock the target, and atomically replace it with a safety copy."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if source_path == destination_path:
        raise ValueError("恢复源不能是当前数据库文件")
    source_metadata = validate_database(source_path, max_schema_version=max_schema_version)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    safety_copy: Path | None = None
    with DatabaseFileLock(destination_path):
        if destination_path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            safety_copy = destination_path.with_name(f"{destination_path.stem}.before-restore-{stamp}.db")
            suffix = 1
            while safety_copy.exists():
                safety_copy = destination_path.with_name(
                    f"{destination_path.stem}.before-restore-{stamp}-{suffix}.db"
                )
                suffix += 1
            _copy_database(destination_path, safety_copy, max_schema_version=max_schema_version)
        _copy_database(source_path, destination_path, max_schema_version=max_schema_version)
    return source_metadata, safety_copy
