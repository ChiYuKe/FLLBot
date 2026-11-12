"""沙盒策略：把配置和单群覆盖解析成一次执行可用的只读快照。"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capabilities import (
    FS_READ,
    FS_WRITE,
    MEMORY_READ,
    NET_READ,
    NET_WRITE,
    PROCESS_EXEC,
    QQ_MANAGE,
    QQ_SEND,
    SYSTEM_READ,
)

DEFAULT_DENY_PATHS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "config.toml",
    "data/admin_token.txt",
    "data/fllbot.db",
    "data/backups",
    "data/backups/*",
    "logs",
    "logs/*",
    ".git",
    ".git/*",
    "*.pem",
    "*.key",
    "*.pfx",
)


def _value(settings: Any, key: str, default: Any) -> Any:
    if settings is None:
        return default
    if isinstance(settings, Mapping):
        value = settings.get(key, default)
    else:
        value = getattr(settings, key, default)
    return default if value is None else value


def _tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    return tuple(str(item).strip() for item in (value or ()) if str(item).strip())


@dataclass(frozen=True)
class SandboxPolicy:
    """不可变策略快照；每次工具调用前重新解析，配置修改立即生效。"""

    enabled: bool = True
    workspace: Path = Path("data/agent_workspace")
    allow_read: bool = True
    allow_write: bool = False
    allow_network: bool = True
    allow_network_write: bool = False
    allow_private_network: bool = False
    allow_qq_send: bool = True
    allow_destructive: bool = False
    allow_process: bool = False
    destructive_requires_superuser: bool = True
    write_requires_addressed: bool = True
    allowed_hosts: tuple[str, ...] = ()
    denied_hosts: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = DEFAULT_DENY_PATHS
    max_read_bytes: int = 200_000
    max_write_bytes: int = 200_000
    max_fetch_bytes: int = 2_000_000
    request_timeout: float = 10.0
    max_redirects: int = 3
    max_calls_per_turn: int = 16
    max_write_calls_per_turn: int = 4
    group_id: int | None = None

    # -- 能力开关 --------------------------------------------------------- #
    def allows(self, capability: str) -> tuple[bool, str]:
        if not self.enabled:
            return False, "sandbox_disabled"
        if capability == FS_READ:
            return (self.allow_read, "read_disabled")
        if capability == FS_WRITE:
            return (self.allow_write, "write_disabled")
        if capability == NET_READ:
            return (self.allow_network, "network_disabled")
        if capability == NET_WRITE:
            return (self.allow_network and self.allow_network_write, "network_write_disabled")
        if capability == QQ_SEND:
            return (self.allow_qq_send, "qq_send_disabled")
        if capability == QQ_MANAGE:
            return (self.allow_destructive, "destructive_disabled")
        if capability == PROCESS_EXEC:
            return (self.allow_process, "process_disabled")
        if capability in (MEMORY_READ, SYSTEM_READ):
            return True, ""
        return False, "capability_unknown"

    # -- 路径 ------------------------------------------------------------- #
    def path_denied(self, relative: str) -> str:
        text = relative.replace("\\", "/").strip("/")
        candidates = {text, text.rsplit("/", 1)[-1]}
        for pattern in self.deny_paths:
            cleaned = pattern.replace("\\", "/").strip()
            if not cleaned:
                continue
            for candidate in candidates:
                if fnmatch.fnmatch(candidate.casefold(), cleaned.casefold()):
                    return pattern
            prefix = cleaned.rstrip("*").rstrip("/")
            if cleaned.endswith("*") and prefix and text.casefold().startswith(prefix.casefold()):
                return pattern
        return ""

    # -- 网络 ------------------------------------------------------------- #
    def host_allowed(self, host: str) -> tuple[bool, str]:
        name = (host or "").strip().casefold().rstrip(".")
        if not name:
            return False, "network_host_missing"
        for pattern in self.denied_hosts:
            if _host_matches(name, pattern):
                return False, "network_host_denied"
        if self.allowed_hosts and not any(_host_matches(name, pattern) for pattern in self.allowed_hosts):
            return False, "network_host_not_allowed"
        return True, ""


def _host_matches(host: str, pattern: str) -> bool:
    item = pattern.strip().casefold().rstrip(".")
    if not item:
        return False
    if item.startswith("*."):
        suffix = item[1:]
        return host.endswith(suffix) and host != suffix.lstrip(".")
    if item.startswith("."):
        return host.endswith(item) or host == item.lstrip(".")
    return host == item


def resolve(settings: Any, root: Path, overrides: Mapping[str, Any] | None = None,
            group_id: int | None = None) -> SandboxPolicy:
    """配置默认值 + 单群覆盖。单群覆盖只影响对应群，不写回全局配置。"""
    overrides = overrides or {}
    workspace = Path(str(_value(settings, "workspace", "data/agent_workspace"))).expanduser()
    if not workspace.is_absolute():
        workspace = (Path(root) / workspace)
    allow_write = bool(_value(settings, "allow_write", False))
    if overrides.get("sandbox_write") is not None:
        allow_write = bool(overrides["sandbox_write"])
    allow_network = bool(_value(settings, "allow_network", True))
    if overrides.get("sandbox_network") is not None:
        allow_network = bool(overrides["sandbox_network"])
    allow_private = bool(_value(settings, "allow_private_network", False))
    if overrides.get("sandbox_private_network") is not None:
        allow_private = bool(overrides["sandbox_private_network"])
    deny = _tuple(_value(settings, "deny_paths", DEFAULT_DENY_PATHS))
    return SandboxPolicy(
        enabled=bool(_value(settings, "enabled", True)),
        workspace=workspace.resolve(strict=False),
        allow_read=bool(_value(settings, "allow_read", True)),
        allow_write=allow_write,
        allow_network=allow_network,
        allow_network_write=bool(_value(settings, "allow_network_write", False)),
        allow_private_network=allow_private,
        allow_qq_send=bool(_value(settings, "allow_qq_send", True)),
        allow_destructive=bool(_value(settings, "allow_destructive", False)),
        allow_process=bool(_value(settings, "allow_process", False)),
        destructive_requires_superuser=bool(_value(settings, "destructive_requires_superuser", True)),
        write_requires_addressed=bool(_value(settings, "write_requires_addressed", True)),
        allowed_hosts=_tuple(_value(settings, "allowed_hosts", ())),
        denied_hosts=_tuple(_value(settings, "denied_hosts", ())),
        deny_paths=deny or DEFAULT_DENY_PATHS,
        max_read_bytes=int(_value(settings, "max_read_bytes", 200_000)),
        max_write_bytes=int(_value(settings, "max_write_bytes", 200_000)),
        max_fetch_bytes=int(_value(settings, "max_fetch_bytes", 2_000_000)),
        request_timeout=float(_value(settings, "request_timeout", 10.0)),
        max_redirects=int(_value(settings, "max_redirects", 3)),
        max_calls_per_turn=int(_value(settings, "max_calls_per_turn", 16)),
        max_write_calls_per_turn=int(_value(settings, "max_write_calls_per_turn", 4)),
        group_id=group_id,
    )


__all__ = ["DEFAULT_DENY_PATHS", "SandboxPolicy", "resolve"]
