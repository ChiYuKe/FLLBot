"""智能体执行沙盒：能力声明、策略、操作裁决、工作区文件与受控联网。

宿主只在工具真正执行前调用这里；沙盒不导入插件、协议、存储或管理接口，
自身也不持有事件循环外的全局状态。
"""

from __future__ import annotations

from .capabilities import (
    CAPABILITIES,
    CAPABILITY_TITLES,
    EMAIL_SEND,
    FS_READ,
    FS_WRITE,
    MEMORY_READ,
    NET_READ,
    NET_WRITE,
    PROCESS_EXEC,
    QQ_MANAGE,
    QQ_SEND,
    QQ_SEND_FILE,
    RISK_LEVELS,
    SCREEN_CAPTURE,
    SYSTEM_READ,
    CapabilityError,
    effective_risk,
    normalize,
)
from .files import SandboxError, SandboxFiles
from .guard import Operation, Verdict, classify, decide
from .network import SandboxNetwork
from .policy import SandboxPolicy, resolve
from .runtime import SandboxRuntime, SandboxService

__all__ = [
    "CAPABILITIES",
    "CAPABILITY_TITLES",
    "EMAIL_SEND",
    "FS_READ",
    "FS_WRITE",
    "MEMORY_READ",
    "NET_READ",
    "NET_WRITE",
    "PROCESS_EXEC",
    "QQ_MANAGE",
    "QQ_SEND",
    "QQ_SEND_FILE",
    "RISK_LEVELS",
    "SCREEN_CAPTURE",
    "SYSTEM_READ",
    "CapabilityError",
    "Operation",
    "SandboxError",
    "SandboxFiles",
    "SandboxNetwork",
    "SandboxPolicy",
    "SandboxRuntime",
    "SandboxService",
    "Verdict",
    "classify",
    "decide",
    "effective_risk",
    "normalize",
    "resolve",
]
