"""工作区文件访问：真实路径围栏、大小限制与敏感路径拒绝。

这里的检查发生在真正读写之前，并且对每次调用重新解析工作区根目录，
避免“检查时是相对路径、执行时变成软链接”这类典型逃逸。
"""

from __future__ import annotations

from pathlib import Path

from .policy import SandboxPolicy


class SandboxError(ValueError):
    """沙盒拒绝或无法完成一次文件操作；``code`` 可安全返回给模型。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class SandboxFiles:
    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy
        self.root = policy.workspace

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

    def list_dir(self, path: str = "", *, limit: int = 200) -> dict:
        if not self.policy.allow_read:
            raise SandboxError("read_disabled", "当前沙盒未开放读取")
        relative = self.relative(path)
        target = self.resolve(relative) if relative else self.root.resolve(strict=False)
        if not target.is_dir():
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
                   encoding: str = "utf-8", max_bytes: int | None = None) -> dict:
        if not isinstance(content, str):
            raise SandboxError("content_invalid", "写入内容必须是文本")
        data = content.encode(encoding, errors="replace")
        limit = min(int(max_bytes or self.policy.max_write_bytes), self.policy.max_write_bytes)
        if len(data) > limit:
            raise SandboxError("content_too_large", "内容超过沙盒单次写入上限")
        relative = self.relative(path)
        if not relative:
            raise SandboxError("path_invalid", "请提供要写入的文件")
        target = self.resolve(relative, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab" if append else "wb") as stream:
            stream.write(data)
        return {"path": relative, "bytes": len(data), "appended": bool(append)}


__all__ = ["SandboxError", "SandboxFiles"]
