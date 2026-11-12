"""工具能力术语表：只描述“这个工具会碰什么”，不做 I/O 和策略判断。

插件在 ``on_ai_tool`` 中显式声明能力；宿主据此把一次模型发起的调用翻译成
可审计的操作意图。未声明能力的旧工具保持原有行为（由权限系统决定）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

FS_READ = "fs.read"
FS_WRITE = "fs.write"
NET_READ = "net.read"
NET_WRITE = "net.write"
QQ_SEND = "qq.send"
QQ_MANAGE = "qq.manage"
MEMORY_READ = "memory.read"
SYSTEM_READ = "system.read"
PROCESS_EXEC = "process.exec"

Risk = Literal["read", "write", "destructive"]

RISK_LEVELS: tuple[str, ...] = ("read", "write", "destructive")
RANK = {"read": 0, "write": 1, "destructive": 2}

CAPABILITY_TITLES: dict[str, str] = {
    FS_READ: "读取沙盒文件",
    FS_WRITE: "写入沙盒文件",
    NET_READ: "读取网络资源",
    NET_WRITE: "向网络提交数据",
    QQ_SEND: "发送 QQ 消息或媒体",
    QQ_MANAGE: "管理群成员或群设置",
    MEMORY_READ: "读取本群记忆",
    SYSTEM_READ: "读取机器人运行状态",
    PROCESS_EXEC: "执行本机程序",
}

RISK_BY_CAPABILITY: dict[str, Risk] = {
    FS_READ: "read",
    FS_WRITE: "write",
    NET_READ: "read",
    NET_WRITE: "write",
    QQ_SEND: "write",
    QQ_MANAGE: "destructive",
    MEMORY_READ: "read",
    SYSTEM_READ: "read",
    PROCESS_EXEC: "destructive",
}

CAPABILITIES = frozenset(CAPABILITY_TITLES)


class CapabilityError(ValueError):
    """插件声明了宿主不认识或互相矛盾的能力。"""


def normalize(values: Iterable[str] | None) -> tuple[str, ...]:
    """校验并去重能力名称；拼写错误在加载插件时就暴露，而不是等模型调用。"""
    result: list[str] = []
    for raw in values or ():
        name = str(raw).strip().lower()
        if not name:
            continue
        if name not in CAPABILITIES:
            raise CapabilityError(f"未知工具能力：{raw}（可用：{', '.join(sorted(CAPABILITIES))}）")
        if name not in result:
            result.append(name)
    return tuple(result)


def default_risk(effect: str) -> Risk:
    """旧工具只有 effect；read_only 视为只读，其余按写操作处理。"""
    return "read" if effect == "read_only" else "write"


def derived_risk(capabilities: Iterable[str]) -> Risk:
    risk: Risk = "read"
    for name in capabilities:
        candidate = RISK_BY_CAPABILITY.get(name, "read")
        if RANK[candidate] > RANK[risk]:
            risk = candidate
    return risk


def effective_risk(effect: str, capabilities: Iterable[str], declared: str | None = None) -> Risk:
    """声明的 risk 不能低于能力推导出的下限，避免插件“降级”危险操作。"""
    capabilities = tuple(capabilities)
    risk = declared or default_risk(effect)
    if risk not in RANK:
        raise CapabilityError(f"未知风险级别：{risk}")
    minimum = derived_risk(capabilities)
    if RANK[risk] < RANK[minimum]:
        raise CapabilityError(f"声明的风险级别 {risk} 低于能力要求 {minimum}")
    return risk  # type: ignore[return-value]
