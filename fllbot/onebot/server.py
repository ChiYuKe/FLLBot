"""反向 WebSocket 服务端：接受 NapCat 连接并管理多账号连接。"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from starlette.websockets import WebSocket

from .connection import OneBotConnection

if TYPE_CHECKING:  # pragma: no cover
    from ..app import FLLBot

RawEventHandler = Callable[[OneBotConnection, dict[str, Any]], Awaitable[None]]


class ConnectionManager:
    """维护所有来自 NapCat 的连接。"""

    def __init__(
        self,
        on_event: RawEventHandler,
        *,
        heartbeat_timeout: float = 120.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._on_event = on_event
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.logger = logger or logging.getLogger("fllbot.onebot")
        self._connections: list[OneBotConnection] = []
        self._monitor: asyncio.Task[None] | None = None

    # -- 查询 ------------------------------------------------------------- #
    def list(self) -> list[OneBotConnection]:
        return list(self._connections)

    def count(self) -> int:
        return len(self._connections)

    def by_self_id(self, self_id: int | None) -> OneBotConnection | None:
        if not self_id:
            return self.primary()
        for conn in self._connections:
            if conn.self_id == int(self_id) and conn.online:
                return conn
        return None

    def primary(self) -> OneBotConnection | None:
        for conn in self._connections:
            if conn.online:
                return conn
        return None

    def summaries(self) -> list[dict[str, Any]]:
        return [conn.summary() for conn in self._connections]

    # -- 生命周期 --------------------------------------------------------- #
    def add(self, conn: OneBotConnection) -> None:
        self._connections.append(conn)
        self.logger.info("NapCat 已连接：self_id=%s role=%s 当前连接数=%d", conn.self_id or "?", conn.client_role or "-", len(self._connections))

    def remove(self, conn: OneBotConnection) -> None:
        if conn in self._connections:
            self._connections.remove(conn)
            self.logger.info("NapCat 连接断开：self_id=%s 剩余连接数=%d", conn.self_id or "?", len(self._connections))

    async def handle_event(self, conn: OneBotConnection, payload: dict[str, Any]) -> None:
        await self._on_event(conn, payload)

    def start(self) -> None:
        if self._monitor is None:
            self._monitor = asyncio.create_task(self._monitor_loop(), name="fllbot-heartbeat")

    async def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            try:
                await self._monitor
            except asyncio.CancelledError:
                pass
            self._monitor = None
        for conn in list(self._connections):
            try:
                await conn.close(code=1001, reason="server shutdown")
            except Exception:  # pragma: no cover
                pass
        self._connections.clear()

    async def _monitor_loop(self) -> None:
        interval = max(5.0, min(30.0, self.heartbeat_timeout / 4 if self.heartbeat_timeout else 15.0))
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            for conn in list(self._connections):
                if conn.closed:
                    self._connections.remove(conn)
                    continue
                idle = now - conn.last_seen_monotonic
                if self.heartbeat_timeout and idle > self.heartbeat_timeout:
                    self.logger.warning(
                        "连接 %s 超过 %.0fs 无心跳，主动断开（可能 NapCat 已退出）", conn.label, idle
                    )
                    try:
                        await conn.close(code=4000, reason="heartbeat timeout")
                    except Exception:  # pragma: no cover
                        pass
                    self._connections.remove(conn)


def create_ws_router(bot: FLLBot) -> APIRouter:
    """注册反向 WS 端点；为兼容不同 NapCat 配置，接受多种路径。"""
    router = APIRouter()

    async def endpoint(websocket: WebSocket) -> None:
        await serve_connection(websocket, bot)

    router.add_api_websocket_route("/onebot/v11/ws", endpoint)
    router.add_api_websocket_route("/onebot/v11/ws/", endpoint)
    router.add_api_websocket_route("/onebot/ws", endpoint)
    router.add_api_websocket_route("/ws", endpoint)
    router.add_api_websocket_route("/", endpoint)
    router.add_api_websocket_route("/{rest:path}", endpoint)
    return router


async def serve_connection(websocket: WebSocket, bot: FLLBot) -> None:
    """处理一次 NapCat 反向连接。"""
    config = bot.config
    expected = config.server.access_token
    offered = _extract_token(websocket)

    if expected and not hmac.compare_digest(str(offered), str(expected)):
        bot.logger.warning("WS 鉴权失败，已拒绝来自 %s 的连接", websocket.client)
        await websocket.accept()
        await websocket.close(code=4401)
        return

    await websocket.accept()
    self_id = _as_int(websocket.headers.get("x-self-id")) or _as_int(websocket.query_params.get("self_id"))
    conn = OneBotConnection(
        websocket,
        on_event=bot.manager.handle_event,
        self_id=self_id,
        client_role=websocket.headers.get("x-client-role", ""),
        client_name=websocket.headers.get("x-client-name") or websocket.headers.get("user-agent", ""),
        call_timeout=float(config.server.call_timeout or 30.0),
        logger=bot.logger.getChild("ws"),
    )
    bot.manager.add(conn)
    bot.hub.publish(
        {
            "type": "connection",
            "online": True,
            "self_id": conn.self_id or None,
            "client_role": conn.client_role,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    )
    try:
        await conn.read_loop()
    except Exception:  # pragma: no cover - 防御式
        bot.logger.exception("WS 连接异常")
    finally:
        bot.manager.remove(conn)
        bot.hub.publish(
            {
                "type": "connection",
                "online": False,
                "self_id": conn.self_id or None,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        )
        if not conn.closed:
            try:
                await conn.close(code=1000)
            except Exception:  # pragma: no cover
                pass


def _extract_token(websocket: WebSocket) -> str:
    for name in ("access_token", "token"):
        value = websocket.query_params.get(name)
        if value:
            return value
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return websocket.headers.get("x-access-token", "")


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


__all__ = ["ConnectionManager", "create_ws_router", "serve_connection"]
