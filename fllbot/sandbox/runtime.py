"""沙盒运行时：把策略、文件、联网和本轮操作额度绑定到一次事件上下文。"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .capabilities import CAPABILITY_TITLES, RANK, CapabilityError, effective_risk
from .files import SandboxError, SandboxFiles
from .guard import Verdict, decide
from .network import SandboxNetwork
from .policy import SandboxPolicy, resolve


class SandboxService:
    """宿主级沙盒入口；每次调用重新解析策略，配置或群覆盖修改立即生效。"""

    def __init__(self, settings: Any, root: Path | str,
                 group_overrides: Callable[[int], Mapping[str, Any]] | None = None,
                 transport: Any = None) -> None:
        self._settings = settings if callable(settings) else (lambda: settings)
        self.root = Path(root)
        self._group_overrides = group_overrides or (lambda group_id: {})
        self.transport = transport
        self.stats: Counter = Counter()
        self.recent: deque[dict[str, Any]] = deque(maxlen=100)
        self.logger = logging.getLogger("fllbot.sandbox")

    def ensure_workspace(self) -> Path | None:
        """启动时创建一次工作区目录；失败只记录警告，不阻止机器人启动。"""
        try:
            workspace = self.policy(None).workspace
            workspace.mkdir(parents=True, exist_ok=True)
            return workspace
        except OSError:
            self.logger.warning("沙盒工作区创建失败，文件工具写入将不可用", exc_info=True)
            return None

    def policy(self, group_id: int | None = None) -> SandboxPolicy:
        overrides = self._group_overrides(group_id) if group_id else {}
        return resolve(self._settings(), self.root, overrides, group_id)

    def runtime(self, ctx: Any) -> SandboxRuntime:
        return SandboxRuntime(self, ctx)

    def record(self, *, tool: str, verdict: Verdict, group_id: int | None) -> None:
        # 旧工具与关闭沙盒时不参与统计，避免把“未声明能力”误当成沙盒放行。
        if verdict.reason in {"legacy_tool", "sandbox_disabled"}:
            return
        self.stats["allowed" if verdict.allowed else "denied"] += 1
        if not verdict.allowed:
            self.stats[f"denied:{verdict.reason}"] += 1
        self.recent.append({
            "at": time.time(),
            "tool": tool,
            "group_id": group_id or 0,
            "allowed": verdict.allowed,
            "reason": verdict.reason,
            "capability": verdict.capability,
            "risk": verdict.risk,
        })

    def status(self) -> dict[str, Any]:
        policy = self.policy(None)
        return {
            "enabled": policy.enabled,
            "workspace": str(policy.workspace),
            "workspace_scope": policy.workspace_scope,
            "capabilities": {
                "read": policy.allow_read,
                "write": policy.allow_write,
                "network": policy.allow_network,
                "network_write": policy.allow_network_write,
                "private_network": policy.allow_private_network,
                "email": policy.allow_email,
                "qq_send": policy.allow_qq_send,
                "destructive": policy.allow_destructive,
                "screen": policy.allow_screen,
                "process": policy.allow_process,
            },
            "network": {
                "allowed_hosts": list(policy.allowed_hosts),
                "denied_hosts": list(policy.denied_hosts),
            },
            "limits": {
                "max_read_bytes": policy.max_read_bytes,
                "max_write_bytes": policy.max_write_bytes,
                "max_file_bytes": policy.max_file_bytes,
                "max_fetch_bytes": policy.max_fetch_bytes,
                "max_calls_per_turn": policy.max_calls_per_turn,
                "max_write_calls_per_turn": policy.max_write_calls_per_turn,
            },
            "stats": dict(self.stats),
            "recent": list(self.recent)[-20:],
        }


class SandboxRuntime:
    def __init__(self, service: SandboxService, ctx: Any) -> None:
        self.service = service
        self.ctx = ctx
        usage = getattr(ctx, "sandbox_usage", None)
        if usage is None:
            usage = {}
            try:
                ctx.sandbox_usage = usage
            except Exception:  # pragma: no cover - 只读上下文
                pass
        self.usage: dict[str, int] = usage

    @property
    def policy(self) -> SandboxPolicy:
        group_id = getattr(self.ctx, "group_id", None)
        return self.service.policy(group_id if getattr(self.ctx, "is_group", False) else None)

    @property
    def workspace_root(self) -> Path:
        """当前会话实际可用的工作区；scene 模式下按群/私聊隔离。"""
        event = getattr(self.ctx, "event", None)
        scene = getattr(event, "scene", "")
        scene_id = getattr(event, "scene_id", 0)
        return self.policy.workspace_for(scene, scene_id)

    @property
    def files(self) -> SandboxFiles:
        return SandboxFiles(self.policy, root=self.workspace_root)

    @property
    def network(self) -> SandboxNetwork:
        return SandboxNetwork(self.policy, transport=self.service.transport)

    def require(self, capability: str) -> None:
        """插件在直接使用某项能力前调用；拒绝时抛出可安全展示的错误。"""
        allowed, reason = self.policy.allows(capability)
        if not allowed:
            title = CAPABILITY_TITLES.get(capability, capability)
            raise SandboxError(reason, f"沙盒未开放：{title}")

    def check(self, spec: Any, arguments: Mapping[str, Any]) -> Verdict:
        capabilities = tuple(getattr(spec, "capabilities", ()) or ())
        risk = getattr(spec, "risk", "read") or "read"
        try:
            if capabilities:
                risk = effective_risk(spec.effect, capabilities, risk)
        except CapabilityError:
            return Verdict(False, "capability_invalid", risk=risk,
                           hint="工具声明的沙盒能力无效，宿主拒绝执行。")
        decision = getattr(self.ctx, "trigger_decision", None)
        kind = str(getattr(decision, "trigger_kind", "") or "")
        if decision is None:
            addressed = True
        else:
            explicit = getattr(decision, "addressed", None)
            addressed = bool(explicit) if explicit is not None else kind in {"at", "prefix"}
        if getattr(self.ctx, "entrypoint", "qq") != "qq":
            addressed = True
        verdict = decide(
            tool=getattr(spec, "qualified_name", getattr(spec, "name", "")),
            spec_capabilities=capabilities,
            spec_risk=risk,
            arguments=arguments,
            policy=self.policy,
            addressed=addressed,
            trigger_kind=kind or "explicit",
            superuser=bool(getattr(self.ctx, "is_superuser", False)),
            group_admin=bool(getattr(self.ctx, "is_group_admin", False)),
            usage=self.usage,
        )
        return verdict

    def commit(self, spec: Any, verdict: Verdict) -> None:
        """只在真正执行后累计额度，避免被拒绝的调用消耗本轮配额。"""
        if not verdict.allowed or not getattr(spec, "capabilities", ()):
            return
        self.usage["calls"] = int(self.usage.get("calls", 0)) + 1
        if RANK.get(verdict.risk, 0) >= RANK["write"]:
            self.usage["writes"] = int(self.usage.get("writes", 0)) + 1
        self.service.record(
            tool=getattr(spec, "qualified_name", getattr(spec, "name", "")),
            verdict=verdict,
            group_id=getattr(self.ctx, "group_id", None),
        )

    def note(self, spec: Any, verdict: Verdict) -> None:
        """被拒绝的调用进入诊断流，但不消耗额度。"""
        if verdict.allowed:
            return
        self.service.record(
            tool=getattr(spec, "qualified_name", getattr(spec, "name", "")),
            verdict=verdict,
            group_id=getattr(self.ctx, "group_id", None),
        )


__all__ = ["SandboxRuntime", "SandboxService"]
