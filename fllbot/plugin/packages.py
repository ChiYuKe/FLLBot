"""Install complete local plugin packages without importing their code or changing host files."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import tomllib
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from packaging.version import Version

from .manifest import validate_name
from .package_lock import PackageLock
from .scaffold import check_package

MAX_FILES = 10000
MAX_BYTES = 100 * 1024 * 1024


@contextmanager
def _workspace(directory: Path, prefix: str):
    work = Path(tempfile.mkdtemp(prefix=prefix, dir=directory))
    try:
        yield work
    except BaseException as error:
        # If even recovery renames fail, never let temporary cleanup destroy the old package.
        if (work / "saved").exists() or (work / "old-history").exists():
            raise OSError(f"文件操作和恢复均受阻，原包已保留在 {work}，请勿删除此目录") from error
        shutil.rmtree(work)
        raise
    else:
        shutil.rmtree(work)


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda _: False)(path))


def _safe_tree(directory: Path) -> None:
    if _linked(directory):
        raise ValueError("插件包不能包含符号链接或目录联接")
    count = size = 0
    for path in directory.rglob("*"):
        if _linked(path) or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError(f"插件包不能包含目录外引用：{path.name}")
        if path.is_file():
            count += 1
            size += path.stat().st_size
        elif not path.is_dir():
            raise ValueError(f"插件包包含不支持的文件：{path.name}")
        if count > MAX_FILES or size > MAX_BYTES:
            raise ValueError("插件包超过 10000 个文件或 100 MiB")


def _extract(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > MAX_BYTES:
            raise ValueError("插件压缩包超过 10000 个文件或 100 MiB")
        seen = set()
        for item in entries:
            path = PurePosixPath(item.filename)
            # ZIP paths must work identically on Windows and POSIX.
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in item.filename
                or ":" in item.filename
                or any(part.endswith((".", " ")) for part in path.parts)
            ):
                raise ValueError(f"压缩包路径无效：{item.filename}")
            key = str(path).casefold()
            if key in seen or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError(f"压缩包有重复路径或符号链接：{item.filename}")
            seen.add(key)
            target = destination.joinpath(*path.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("压缩包路径越界")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as input_file, target.open("xb") as output_file:
                    shutil.copyfileobj(input_file, output_file)


def _package_root(source: Path) -> Path:
    if (source / "plugin.toml").is_file():
        return source
    children = list(source.iterdir())
    if len(children) == 1 and children[0].is_dir() and (children[0] / "plugin.toml").is_file():
        return children[0]
    raise ValueError("来源根目录或 ZIP 的唯一顶层目录必须包含 plugin.toml")


class PackageStore:
    """Stopped-host operations; .previous keeps the last replaced/removed complete package."""

    def __init__(self, directory: Path):
        self.directory = directory.expanduser().resolve()

    def _target(self, name: str) -> Path:
        validate_name(name)
        target = self.directory / name
        if _linked(target):
            raise ValueError("目标插件不能是符号链接或目录联接")
        return target

    def _history(self, name: str) -> Path:
        history = self.directory / ".previous"
        if _linked(history):
            raise ValueError("插件备份目录不能是符号链接或目录联接")
        history.mkdir(exist_ok=True)
        target = history / name
        if _linked(target):
            raise ValueError("插件备份不能是符号链接或目录联接")
        return target

    def install(self, source: Path, *, upgrade: bool = False) -> Path:
        source = source.expanduser().absolute()
        if _linked(source):
            raise ValueError("插件来源不能是符号链接或目录联接")
        source = source.resolve()
        # Stage outside discovery; the running loader never sees partial packages.
        with PackageLock(self.directory), _workspace(self.directory, ".install-") as work:
            incoming = work / "incoming"
            if source.is_dir():
                if self.directory.is_relative_to(source):
                    raise ValueError("插件来源不能包含安装目录")
                _safe_tree(source)
                shutil.copytree(source, incoming, ignore=shutil.ignore_patterns("__pycache__", ".git", ".venv"))
            elif source.is_file() and source.suffix.lower() == ".zip":
                incoming.mkdir()
                _extract(source, incoming)
            else:
                raise ValueError("安装来源必须是本地插件目录或 ZIP 文件")
            package = _package_root(incoming)
            # Repository/archive folder names need not match the stable manifest name.
            metadata = tomllib.loads((package / "plugin.toml").read_text(encoding="utf-8-sig"))
            name = metadata.get("name", "")
            validate_name(name)
            (work / "ready").mkdir()
            staged = work / "ready" / name
            package.rename(staged)
            manifest = check_package(staged)
            manifest.check_environment()
            target = self._target(name)
            if (self.directory / f"{name}.py").exists():
                raise ValueError(f"同名旧式单文件插件已存在：{name}")
            if upgrade:
                if not target.is_dir():
                    raise ValueError(f"插件尚未安装：{name}，请使用 install")
                _safe_tree(target)
                previous = check_package(target)
                if Version(manifest.version) <= Version(previous.version):
                    raise ValueError("升级包版本必须高于已安装版本；恢复旧版请使用 rollback")
            elif target.exists():
                raise ValueError(f"插件已存在：{name}，请使用 upgrade")
            history = self._history(name)
            if history.exists():
                _safe_tree(history)
            # Keep an existing backup until the new directory is in place.
            saved = work / "saved"
            if target.exists():
                target.rename(saved)
            try:
                staged.rename(target)
            except BaseException:
                if saved.exists():
                    saved.rename(target)
                raise
            if saved.exists():
                try:
                    if history.exists():
                        history.rename(work / "old-history")
                    saved.rename(history)
                except BaseException:
                    target.rename(work / "rejected")
                    saved.rename(target)
                    if (work / "old-history").exists():
                        (work / "old-history").rename(history)
                    raise
            elif history.exists():
                history.rename(work / "old-history")
            return target

    def remove(self, name: str) -> Path:
        with PackageLock(self.directory):
            target = self._target(name)
            if not target.is_dir():
                raise ValueError(f"用户插件未安装：{name}")
            _safe_tree(target)
            history = self._history(name)
            if history.exists():
                _safe_tree(history)
            with _workspace(self.directory, ".remove-") as work:
                old = work / "old-history"
                if history.exists():
                    history.rename(old)
                try:
                    target.rename(history)
                except BaseException:
                    if old.exists():
                        old.rename(history)
                    raise
            return history

    def rollback(self, name: str) -> Path:
        with PackageLock(self.directory):
            target, history = self._target(name), self._history(name)
            if not history.is_dir():
                raise ValueError(f"没有可恢复的插件备份：{name}")
            _safe_tree(history)
            manifest = check_package(history)
            manifest.check_environment()
            with _workspace(self.directory, ".rollback-") as work:
                saved = work / "saved"
                if target.exists():
                    _safe_tree(target)
                    target.rename(saved)
                try:
                    history.rename(target)
                except BaseException:
                    if saved.exists():
                        saved.rename(target)
                    raise
                if saved.exists():
                    try:
                        saved.rename(history)
                    except BaseException:
                        target.rename(history)
                        saved.rename(target)
                        raise
            return target
