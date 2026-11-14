"""沙盒联网：只允许 http(s)、按主机白/黑名单与解析结果拒绝内网地址。

裁决阶段做文本级检查；这里在真正连接前再解析 DNS 并逐个地址核对，
每次重定向都重新校验，降低 DNS 重绑定与跳转绕过的风险。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .files import SandboxError
from .policy import SandboxPolicy

TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml",
              "application/javascript", "application/problem+json")
DEFAULT_HEADERS = {
    "User-Agent": "FLLBot-Sandbox/1.0 (+https://github.com/)",
    "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.5",
}


def _private(address) -> bool:
    return (address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast or address.is_unspecified)


def _literal(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


class SandboxNetwork:
    def __init__(self, policy: SandboxPolicy, *, transport: httpx.AsyncBaseTransport | None = None,
                 resolver: Any = None) -> None:
        self.policy = policy
        self.transport = transport
        # 生产环境使用系统 DNS；测试或自定义部署可注入解析器，检查逻辑保持一致。
        self.resolver = resolver

    def parse(self, url: str) -> tuple[str, str, int]:
        text = str(url or "").strip()
        if len(text) > 4000:
            raise SandboxError("url_invalid", "链接过长")
        try:
            parts = urlsplit(text)
        except ValueError:
            raise SandboxError("url_invalid", "链接格式无效") from None
        if parts.scheme.casefold() not in {"http", "https"}:
            raise SandboxError("url_scheme_denied", "只允许 http 或 https 链接")
        if parts.username or parts.password:
            raise SandboxError("url_credentials_denied", "链接不能包含用户名或密码")
        host = (parts.hostname or "").strip()
        if not host:
            raise SandboxError("url_invalid", "链接缺少主机名")
        allowed, reason = self.policy.host_allowed(host)
        if not allowed:
            raise SandboxError(reason, "该主机不在沙盒允许的联网范围内")
        try:
            port = parts.port or (443 if parts.scheme.casefold() == "https" else 80)
        except ValueError:
            raise SandboxError("url_invalid", "链接端口无效") from None
        return parts.scheme.casefold(), host, int(port)

    async def validate(self, url: str) -> str:
        """校验并通过 DNS 解析确认目标不是内网地址；返回原始链接。"""
        _, host, port = self.parse(url)
        literal = _literal(host)
        if literal is not None:
            if not self.policy.allow_private_network and _private(literal):
                raise SandboxError("network_private_denied", "沙盒禁止访问本机、局域网或保留地址")
            return str(url).strip()
        if not self.policy.allow_private_network and (
            host.casefold() in {"localhost", "localhost.localdomain"}
            or host.casefold().endswith((".local", ".internal"))
        ):
            raise SandboxError("network_private_denied", "沙盒禁止访问本机或内网主机")
        try:
            if self.resolver is not None:
                resolved = self.resolver(host, port)
                infos = await resolved if asyncio.iscoroutine(resolved) else resolved
                addresses = [ipaddress.ip_address(str(item)) for item in infos]
            else:
                infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
                addresses = []
                for info in infos:
                    try:
                        addresses.append(ipaddress.ip_address(info[4][0]))
                    except ValueError:
                        continue
        except (OSError, ValueError):
            raise SandboxError("network_dns_failed", "无法解析该主机名") from None
        if not addresses:
            raise SandboxError("network_dns_failed", "该主机没有可用地址")
        if not self.policy.allow_private_network and any(_private(item) for item in addresses):
            raise SandboxError("network_private_denied", "该主机解析到内网或保留地址")
        return str(url).strip()

    def _allowed_type(self, content_type: str) -> bool:
        value = (content_type or "").split(";", 1)[0].strip().casefold()
        return not value or value.startswith(TEXT_TYPES) or value.endswith(("+json", "+xml"))

    async def fetch(self, url: str, *, max_bytes: int | None = None,
                    headers: dict[str, str] | None = None, timeout: float | None = None) -> dict:
        return await self._fetch(url, max_bytes=max_bytes, headers=headers, timeout=timeout,
                                 text_only=True)

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = None,
                          headers: dict[str, str] | None = None,
                          timeout: float | None = None) -> dict:
        """下载原始字节；仍执行同一套 URL、主机、DNS、重定向与大小检查。"""
        return await self._fetch(url, max_bytes=max_bytes, headers=headers, timeout=timeout,
                                 text_only=False)

    async def _fetch(self, url: str, *, max_bytes: int | None = None,
                     headers: dict[str, str] | None = None, timeout: float | None = None,
                     text_only: bool = True) -> dict:
        if not self.policy.allow_network:
            raise SandboxError("network_disabled", "当前沙盒未开放联网")
        # 文本读取受 max_fetch_bytes 约束；二进制下载用于保存文件，使用 max_file_bytes。
        ceiling = self.policy.max_fetch_bytes if text_only else self.policy.max_file_bytes
        limit = min(int(max_bytes or ceiling), ceiling)
        request_headers = {**DEFAULT_HEADERS, **(headers or {})}
        current = await self.validate(url)
        hops = 0
        async with httpx.AsyncClient(
            timeout=float(timeout or self.policy.request_timeout),
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            while True:
                async with client.stream("GET", current, headers=request_headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "")
                        if not location:
                            raise SandboxError("network_redirect_invalid", "重定向缺少目标地址")
                        if hops >= self.policy.max_redirects:
                            raise SandboxError("network_redirect_limit", "重定向次数超过沙盒上限")
                        hops += 1
                        current = await self.validate(urljoin(current, location))
                        continue
                    if 300 <= response.status_code < 400:
                        raise SandboxError("network_redirect_invalid", "该重定向不受沙盒支持")
                    if response.status_code >= 400:
                        raise SandboxError("network_http_error", f"目标站点返回 {response.status_code}")
                    content_type = response.headers.get("content-type", "")
                    if text_only and not self._allowed_type(content_type):
                        raise SandboxError("network_content_type", "沙盒只读取文本类响应")
                    chunks, size = [], 0
                    # 多读 1 字节用于区分“刚好等于上限”和“确实被截断”。
                    cap = limit + 1
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        remaining = cap - size
                        if remaining <= 0:
                            break
                        chunks.append(chunk[:remaining])
                        size += min(len(chunk), remaining)
                        if size >= cap:
                            break
                    raw = b"".join(chunks)
                    truncated = len(raw) > limit
                    raw = raw[:limit]
                    encoding = response.encoding or "utf-8"
                    result = {
                        "url": current,
                        "status": response.status_code,
                        "content_type": content_type.split(";", 1)[0],
                        "bytes": len(raw),
                        "truncated": truncated,
                        **({"notice": "响应超过沙盒单次读取上限，只返回开头部分。"} if truncated else {}),
                    }
                    if text_only:
                        try:
                            result["text"] = raw.decode(encoding, errors="replace")
                        except LookupError:
                            result["text"] = raw.decode("utf-8", errors="replace")
                    else:
                        result["data"] = raw
                    return result


__all__ = ["SandboxNetwork"]
