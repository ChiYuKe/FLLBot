"""操作裁决：把模型请求的工具调用翻译成能力、风险和资源目标，再按策略放行或拒绝。

这里是纯函数，不做 I/O，也不同步请求模型。DNS 解析和真实文件访问分别由
``network`` 与 ``files`` 在真正执行时再次校验，避免检查与使用之间被绕过。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

from .capabilities import FS_WRITE, NET_WRITE, PROCESS_EXEC, QQ_MANAGE, RANK, Risk
from .policy import SandboxPolicy

PATH_KEYS = {
    "path", "file", "filename", "dir", "directory", "folder", "destination",
    "target", "source", "source_path", "target_path", "output", "input",
}
URL_KEYS = {"url", "uri", "link", "href", "endpoint", "webpage", "page"}


@dataclass(frozen=True)
class Operation:
    capabilities: tuple[str, ...]
    risk: Risk
    paths: tuple[str, ...]
    urls: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    capability: str = ""
    risk: str = ""
    hint: str = ""
    denied_path: str = ""
    denied_host: str = ""

    def payload(self) -> dict[str, Any]:
        """只回传模型可理解的原因，不回显参数值或本机绝对路径。"""
        data: dict[str, Any] = {"error": "tool_blocked", "reason": self.reason}
        if self.capability:
            data["capability"] = self.capability
        if self.risk:
            data["risk"] = self.risk
        if self.hint:
            data["hint"] = self.hint
        if self.denied_path:
            data["path"] = self.denied_path
        if self.denied_host:
            data["host"] = self.denied_host
        return data


def _strings(value: Any, depth: int = 0) -> Iterable[tuple[str, str]]:
    """展开参数中的键值对；最多 4 层，避免异常结构拖慢裁决。"""
    if depth > 4:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, str):
                yield str(key), item
            else:
                yield from _strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                yield "", item
            else:
                yield from _strings(item, depth + 1)


def _is_path_like_key(key: str) -> bool:
    name = key.casefold()
    return name in PATH_KEYS or name.endswith(("_path", "_file", "_dir", "_folder"))


def classify(arguments: Mapping[str, Any], capabilities: Iterable[str], risk: str) -> Operation:
    paths: list[str] = []
    urls: list[str] = []
    for key, value in _strings(arguments):
        text = value.strip()
        if not text:
            continue
        lowered = text.casefold()
        if _is_path_like_key(key) and lowered not in {"auto", "none", "default"}:
            if text not in paths:
                paths.append(text)
        # 只按参数名识别链接；正文里出现的 URL 是数据，不是本次网络访问目标。
        if key.casefold() in URL_KEYS:
            if text not in urls:
                urls.append(text)
    return Operation(tuple(capabilities), risk, tuple(paths), tuple(urls))  # type: ignore[arg-type]


def _unsafe_path(value: str) -> str:
    text = unquote(value.strip())
    if "\x00" in text:
        return "path_invalid"
    if text.startswith("~"):
        return "path_absolute"
    windows, posix = PureWindowsPath(text), PurePosixPath(text)
    if windows.is_absolute() or posix.is_absolute() or windows.drive:
        return "path_absolute"
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    if any(part == ".." for part in parts):
        return "path_escape"
    return ""


def _ip_literal(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _non_public(address) -> bool:
    return (address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast or address.is_unspecified)


def _check_url(url: str, policy: SandboxPolicy) -> Verdict:
    try:
        parts = urlsplit(url)
    except ValueError:
        return Verdict(False, "url_invalid", hint="链接格式无效，请提供完整的 http(s) 地址。")
    if parts.scheme.casefold() not in {"http", "https"}:
        return Verdict(False, "url_scheme_denied", hint="只允许 http 或 https 链接。")
    if parts.username or parts.password:
        return Verdict(False, "url_credentials_denied", hint="链接不能包含用户名或密码。")
    host = (parts.hostname or "").strip()
    if not host:
        return Verdict(False, "url_invalid", hint="链接缺少主机名。")
    allowed, reason = policy.host_allowed(host)
    if not allowed:
        return Verdict(False, reason, denied_host=host,
                       hint="该主机不在沙盒允许的联网范围内。")
    literal = _ip_literal(host)
    if not policy.allow_private_network and (literal is not None and _non_public(literal)
                                             or host.casefold() in {"localhost", "localhost.localdomain"}
                                             or host.casefold().endswith((".local", ".internal"))):
        return Verdict(False, "network_private_denied", denied_host=host,
                       hint="沙盒禁止访问本机、局域网或保留地址。")
    return Verdict(True, "")


def decide(*, tool: str, spec_capabilities: Iterable[str], spec_risk: str,
           arguments: Mapping[str, Any], policy: SandboxPolicy, addressed: bool,
           trigger_kind: str, superuser: bool, group_admin: bool,
           usage: Mapping[str, int] | None = None) -> Verdict:
    """返回放行或拒绝的完整理由；调用方只在放行后真正执行工具。"""
    capabilities = tuple(spec_capabilities)
    if not policy.enabled:
        return Verdict(True, "sandbox_disabled")
    if not capabilities:
        # 未声明能力的旧工具保持原有权限行为；沙盒只约束显式声明的能力。
        return Verdict(True, "legacy_tool")
    risk = spec_risk or "read"
    for capability in capabilities:
        allowed, reason = policy.allows(capability)
        if not allowed:
            return Verdict(False, reason, capability=capability, risk=risk,
                           hint="当前沙盒策略没有开放这项能力，请改用已有结果或说明限制。")
    operation = classify(arguments, capabilities, risk)
    for path in operation.paths:
        problem = _unsafe_path(path)
        if problem:
            return Verdict(False, problem, capability=capabilities[0], risk=risk,
                           hint="只能用沙盒工作区内的相对路径，不能跳到工作区外。")
        if policy.path_denied(path):
            return Verdict(False, "path_denied", capability=capabilities[0], risk=risk,
                           denied_path=path, hint="该路径属于沙盒禁止访问的敏感文件。")
    for url in operation.urls:
        verdict = _check_url(url, policy)
        if not verdict.allowed:
            return Verdict(False, verdict.reason, capability=capabilities[0], risk=risk,
                           denied_host=verdict.denied_host, hint=verdict.hint)
    if RANK[risk] >= RANK["write"]:
        side_effect_caps = {FS_WRITE, NET_WRITE, QQ_MANAGE, PROCESS_EXEC}
        if (policy.write_requires_addressed and not addressed
                and side_effect_caps.intersection(capabilities)):
            return Verdict(False, "automatic_side_effect", capability=capabilities[0], risk=risk,
                           hint="自动参与时不执行写入或管理操作；需要群友明确请求。")
        if policy.destructive_requires_superuser and risk == "destructive" and not superuser:
            return Verdict(False, "authority_required", capability=capabilities[0], risk=risk,
                           hint="该操作风险较高，只允许超级管理员发起。")
        if QQ_MANAGE in capabilities and not (superuser or group_admin):
            return Verdict(False, "authority_required", capability=QQ_MANAGE, risk=risk,
                           hint="群管理操作只允许群主、管理员或机器人超级管理员发起。")
    if usage is not None:
        if int(usage.get("calls", 0)) >= policy.max_calls_per_turn:
            return Verdict(False, "turn_quota_exceeded", capability=capabilities[0], risk=risk,
                           hint="本轮沙盒操作次数已达上限，请使用已有结果收尾。")
        if RANK[risk] >= RANK["write"] and int(usage.get("writes", 0)) >= policy.max_write_calls_per_turn:
            return Verdict(False, "turn_write_quota_exceeded", capability=capabilities[0], risk=risk,
                           hint="本轮写入类操作次数已达上限，请先说明已完成部分。")
    return Verdict(True, "allowed", capability=capabilities[0], risk=risk)


__all__ = ["Operation", "Verdict", "classify", "decide"]
