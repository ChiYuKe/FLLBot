"""Cross-process exclusion between a running loader and package file operations."""

from __future__ import annotations

import os
from pathlib import Path


class PackageLock:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = (self.directory / ".packages.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise ValueError("插件目录正在使用，请先停止 FLLBot 或等待其他插件操作完成") from error
        self.handle = handle

    def release(self):
        handle, self.handle = self.handle, None
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

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()
