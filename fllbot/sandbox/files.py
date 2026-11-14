"""工作区文件访问：真实路径围栏、大小限制与敏感路径拒绝。

这里的检查发生在真正读写之前，并且对每次调用重新解析工作区根目录，
避免“检查时是相对路径、执行时变成软链接”这类典型逃逸。
"""

from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path

from .policy import SandboxPolicy


class SandboxError(ValueError):
    """沙盒拒绝或无法完成一次文件操作；``code`` 可安全返回给模型。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class SandboxFiles:
    def __init__(self, policy: SandboxPolicy, root: Path | None = None) -> None:
        self.policy = policy
        self.root = Path(root) if root is not None else policy.workspace

    # -- 路径 ------------------------------------------------------------- #
    def relative(self, path: str | Path) -> str:
        """规范化成工作区内的 POSIX 相对路径。"""
        raw = str(path or "").strip().replace("\\", "/")
        if not raw:
            return ""
        if raw.startswith(("/", "~")) or (len(raw) > 1 and raw[1] == ":"):
            raise SandboxError("path_absolute", "只能使用工作区内的相对路径")
        if "\x00" in raw:
            raise SandboxError("path_invalid", "路径包含非法字符")
        parts = [part for part in raw.split("/") if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise SandboxError("path_escape", "路径不能跳出工作区")
        return "/".join(parts)

    def resolve(self, path: str | Path, *, write: bool = False) -> Path:
        if write and not self.policy.allow_write:
            raise SandboxError("write_disabled", "当前沙盒未开放写入")
        relative = self.relative(path)
        denied = self.policy.path_denied(relative) if relative else ""
        if denied:
            raise SandboxError("path_denied", "该路径属于沙盒禁止访问的敏感文件")
        root = self.root.resolve(strict=False)
        target = (root / relative).resolve(strict=False) if relative else root
        try:
            target.relative_to(root)
        except ValueError:
            raise SandboxError("path_escape", "路径不能跳到工作区之外") from None
        return target

    def ensure(self) -> Path:
        root = self.root.resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        return root

    # -- 读 --------------------------------------------------------------- #
    def read_text(self, path: str, *, max_bytes: int | None = None, encoding: str = "utf-8") -> dict:
        if not self.policy.allow_read:
            raise SandboxError("read_disabled", "当前沙盒未开放读取")
        relative = self.relative(path)
        if not relative:
            raise SandboxError("path_invalid", "请提供要读取的文件")
        target = self.resolve(relative)
        if not target.is_file():
            raise SandboxError("not_found", "工作区里没有这个文件")
        limit = min(int(max_bytes or self.policy.max_read_bytes), self.policy.max_read_bytes)
        with target.open("rb") as stream:
            data = stream.read(limit + 1)
        truncated = len(data) > limit
        text = data[:limit].decode(encoding, errors="replace")
        return {
            "path": relative,
            "content": text,
            "bytes": len(data[:limit]),
            "truncated": truncated,
            **({"notice": "文件超过沙盒单次读取上限，只返回开头部分。"} if truncated else {}),
        }

    def read_bytes(self, path: str, *, max_bytes: int | None = None) -> dict:
        """读取工作区文件原始字节，供复制或作为附件发送；不截断，超限直接拒绝。"""
        if not self.policy.allow_read:
            raise SandboxError("read_disabled", "当前沙盒未开放读取")
        relative = self.relative(path)
        if not relative:
            raise SandboxError("path_invalid", "请提供要读取的文件")
        target = self.resolve(relative)
        if not target.is_file():
            raise SandboxError("not_found", "工作区里没有这个文件")
        limit = min(int(max_bytes or self.policy.max_file_bytes), self.policy.max_file_bytes)
        size = target.stat().st_size
        if size > limit:
            raise SandboxError("file_too_large", "文件超过沙盒允许的大小")
        return {"path": relative, "data": target.read_bytes(), "bytes": size}

    def stat(self, path: str = "") -> dict:
        if not self.policy.allow_read:
            raise SandboxError("read_disabled", "当前沙盒未开放读取")
        relative = self.relative(path)
        target = self.resolve(relative) if relative else self.root.resolve(strict=False)
        if not target.exists():
            raise SandboxError("not_found", "工作区里没有这个路径")
        info = target.stat()
        return {
            "path": relative,
            "type": "dir" if target.is_dir() else "file",
            "bytes": info.st_size if target.is_file() else 0,
            "modified_at": info.st_mtime,
        }

    def make_dir(self, path: str) -> dict:
        relative = self.relative(path)
        if not relative:
            raise SandboxError("path_invalid", "请提供要创建的目录")
        target = self.resolve(relative, write=True)
        if target.exists() and not target.is_dir():
            raise SandboxError("path_conflict", "目标已存在且不是目录")
        target.mkdir(parents=True, exist_ok=True)
        return {"path": relative, "created": True}

    def move(self, source: str, destination: str, *, overwrite: bool = False) -> dict:
        src_rel, dst_rel = self.relative(source), self.relative(destination)
        if not src_rel or not dst_rel:
            raise SandboxError("path_invalid", "请提供源路径和目标路径")
        src = self.resolve(src_rel)
        if not src.exists():
            raise SandboxError("not_found", "源文件或目录不存在")
        dst = self.resolve(dst_rel, write=True)
        if dst.exists() and not overwrite:
            raise SandboxError("path_exists", "目标已存在；需要覆盖时显式设置 overwrite")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        shutil.move(str(src), str(dst))
        return {"source": src_rel, "destination": dst_rel}

    def copy(self, source: str, destination: str, *, overwrite: bool = False) -> dict:
        if not self.policy.allow_read:
            raise SandboxError("read_disabled", "当前沙盒未开放读取")
        src_rel, dst_rel = self.relative(source), self.relative(destination)
        if not src_rel or not dst_rel:
            raise SandboxError("path_invalid", "请提供源路径和目标路径")
        src = self.resolve(src_rel)
        if not src.is_file():
            raise SandboxError("not_found", "只能复制工作区内的文件")
        dst = self.resolve(dst_rel, write=True)
        if dst.exists() and not overwrite:
            raise SandboxError("path_exists", "目标已存在；需要覆盖时显式设置 overwrite")
        dest_parent = dst.parent
        dest_parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return {"source": src_rel, "destination": dst_rel, "bytes": dst.stat().st_size}

    def delete(self, path: str, *, recursive: bool = False) -> dict:
        relative = self.relative(path)
        if not relative:
            raise SandboxError("path_invalid", "不能删除工作区根目录")
        target = self.resolve(relative, write=True)
        if not target.exists():
            raise SandboxError("not_found", "工作区里没有这个路径")
        if target.is_dir():
            if not recursive:
                raise SandboxError("path_not_empty", "目录需要 recursive=true 才能删除")
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"path": relative, "deleted": True}

    def search(self, query: str = "", path: str = "", *, pattern: str = "",
               max_results: int = 50) -> dict:
        """在工作区内做有界文本搜索；扫描文件数、单文件大小和结果数都有上限。"""
        if not self.policy.allow_read:
            raise SandboxError("read_disabled", "当前沙盒未开放读取")
        needle = (query or "").strip()
        glob = (pattern or "").strip().replace("\\", "/")
        if not needle and not glob:
            raise SandboxError("query_required", "请提供搜索关键词或文件名模式")
        if len(needle) > 200 or len(glob) > 200:
            raise SandboxError("query_invalid", "搜索条件过长")
        relative = self.relative(path)
        target = self.resolve(relative) if relative else self.root.resolve(strict=False)
        if not target.is_dir():
            raise SandboxError("not_found", "工作区里没有这个目录")
        limit = max(1, min(int(max_results), 100))
        base = self.root.resolve(strict=False)
        results: list[dict] = []
        scanned = 0
        truncated = False
        for item in sorted(target.rglob("*")):
            if len(results) >= limit or scanned >= 500:
                truncated = True
                break
            if not item.is_file():
                continue
            try:
                resolved = item.resolve(strict=True)
                rel = resolved.relative_to(base).as_posix()
            except (OSError, ValueError):
                continue
            if self.policy.path_denied(rel):
                continue
            if glob and not (fnmatch.fnmatch(rel.casefold(), glob.casefold())
                             or fnmatch.fnmatch(item.name.casefold(), glob.casefold())):
                continue
            scanned += 1
            try:
                if item.stat().st_size > self.policy.max_read_bytes:
                    continue
                data = item.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:1024]:
                continue
            if not needle:
                results.append({"path": rel, "line": 0, "text": item.name})
                continue
            text = data.decode("utf-8", errors="ignore")
            for number, line in enumerate(text.splitlines(), 1):
                if needle.casefold() not in line.casefold():
                    continue
                results.append({"path": rel, "line": number, "text": line.strip()[:300]})
                if len(results) >= limit:
                    truncated = True
                    break
        return {
            "query": needle, "pattern": glob, "path": relative,
            "results": results, "scanned": scanned, "truncated": truncated,
        }

    def list_dir(self, path: str = "", *, limit: int = 200) -> dict:
        if not self.policy.allow_read:
            raise SandboxError("read_disabled", "当前沙盒未开放读取")
        relative = self.relative(path)
        target = self.resolve(relative) if relative else self.root.resolve(strict=False)
        if not target.is_dir():
            if not relative and not target.exists():
                # 新群/新会话尚未写入过文件：返回空列表比报错更符合预期。
                return {"path": "", "entries": [], "root": str(self.root)}
            raise SandboxError("not_found", "工作区里没有这个目录")
        entries = []
        for item in sorted(target.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.casefold())):
            if len(entries) >= max(1, min(limit, 500)):
                break
            try:
                stat = item.stat()
            except OSError:
                continue
            entry = {
                "name": item.name,
                "path": f"{relative}/{item.name}".strip("/"),
                "type": "dir" if item.is_dir() else "file",
            }
            if item.is_file():
                entry["bytes"] = stat.st_size
            entries.append(entry)
        return {"path": relative, "entries": entries}

    def exists(self, path: str) -> bool:
        try:
            return self.resolve(path).exists()
        except SandboxError:
            return False

    # -- 写 --------------------------------------------------------------- #
    def write_text(self, path: str, content: str, *, append: bool = False,
                   encoding: str = "utf-8", max_bytes: int | None = None,
                   overwrite: bool = True) -> dict:
        if not isinstance(content, str):
            raise SandboxError("content_invalid", "写入内容必须是文本")
        return self.write_bytes(path, content.encode(encoding, errors="replace"),
                                append=append, max_bytes=max_bytes, overwrite=overwrite)

    def write_bytes(self, path: str, data: bytes, *, append: bool = False,
                    max_bytes: int | None = None, overwrite: bool = True) -> dict:
        if not isinstance(data, (bytes, bytearray)):
            raise SandboxError("content_invalid", "写入内容必须是字节")
        limit = int(max_bytes) if max_bytes is not None else self.policy.max_write_bytes
        limit = min(limit, self.policy.max_file_bytes)
        if len(data) > limit:
            raise SandboxError("content_too_large", "内容超过沙盒单次写入上限")
        relative = self.relative(path)
        if not relative:
            raise SandboxError("path_invalid", "请提供要写入的文件")
        target = self.resolve(relative, write=True)
        if target.exists() and not overwrite:
            raise SandboxError("path_exists", "目标已存在；需要覆盖时显式设置 overwrite")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab" if append else "wb") as stream:
            stream.write(data)
        return {"path": relative, "bytes": len(data), "appended": bool(append)}


__all__ = ["SandboxError", "SandboxFiles"]
