"""应用装配：持久化、连接管理、事件分发、插件、LLM 与管理 API 的组装与生命周期。"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import AppConfig, ConfigStore, mask_secrets, resolve_path
from .dispatcher import Dispatcher
from .events import EventHub
from .llm.chat import ChatService
from .llm.personas import PersonaStore
from .logging_setup import configure_logging
from .onebot.connection import OneBotConnection
from .onebot.models import MessageEvent, MetaEvent, parse_event
from .onebot.server import ConnectionManager, create_ws_router
from .permission import PermissionManager
from .plugin.loader import PluginLoader
from .storage import Storage


class FLLBot:
    """整个后端的根对象。"""

    def __init__(self, config_store: ConfigStore, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.config_store = config_store
        self.logger = logging.getLogger("fllbot")

        config = config_store.current
        self.storage = Storage(resolve_path(self.root, config.storage.path))
        self.hub = EventHub()
        self.permissions = PermissionManager(self)
        self.dispatcher = Dispatcher(self)
        self.personas = PersonaStore(
            [Path(__file__).parent / "personas", self.root / "personas"],
            default_name=config.llm.persona or "fllbot",
        )
        self.chat = ChatService(self)
        self.manager = ConnectionManager(
            self.on_raw_event,
            heartbeat_timeout=config.server.heartbeat_timeout,
            logger=self.logger.getChild("onebot"),
        )
        self.plugins = PluginLoader(
            self,
            [resolve_path(self.root, item) for item in config.plugin.dirs],
            load_builtin=config.plugin.builtin,
            hot_reload=config.plugin.hot_reload,
            watch_interval=config.plugin.watch_interval,
        )

        self.admin_token: str = ""
        self.started_at: float = 0.0
        self.started: bool = False
        self._start_lock = asyncio.Lock()

    # -- 配置 ------------------------------------------------------------- #
    @property
    def config(self) -> AppConfig:
        return self.config_store.current

    async def update_config(self, patch: dict[str, Any]) -> AppConfig:
        """应用运行时配置覆盖并持久化。"""
        config = self.config_store.patch(patch)
        await self.storage.meta_set("config_overrides", self.config_store.overrides)
        self._apply_runtime_config(config)
        self.logger.info("运行时配置已更新：%s", ", ".join(sorted(patch)))
        return config

    def _apply_runtime_config(self, config: AppConfig) -> None:
        if config.admin.token:
            self.admin_token = config.admin.token
        self.manager.heartbeat_timeout = float(config.server.heartbeat_timeout or 0)
        logging.getLogger().setLevel(getattr(logging, str(config.logging.level).upper(), logging.INFO))

    # -- 生命周期 --------------------------------------------------------- #
    async def start(self) -> None:
        async with self._start_lock:
            if self.started:
                return
            config = self.config
            configure_logging(
                level=config.logging.level,
                log_dir=resolve_path(self.root, config.logging.dir),
                json_mode=config.logging.json_lines,
                filename=config.logging.file,
                ring_size=config.logging.ring_size,
            )

            await self.storage.connect()
            overrides = await self.storage.meta_get("config_overrides", {})
            if isinstance(overrides, dict) and overrides:
                self.config_store.restore(overrides)
                self._apply_runtime_config(self.config)
                self.logger.info("已恢复运行时的配置覆盖")

            self._ensure_admin_token()
            self.personas.load()
            await self.permissions.load()
            await self.plugins.load_all()
            self.manager.heartbeat_timeout = float(self.config.server.heartbeat_timeout or 0)
            await self.dispatcher.startup()
            self.plugins.start_watcher()
            self.manager.start()

            self.started_at = time.time()
            self.started = True
            self.logger.info(
                "FLLBot %s 已启动：WS %s | 插件 %d 个 | LLM %s",
                __version__,
                self.config.server.ws_path,
                len(self.plugins.names()),
                "已启用" if self.chat.available else "未启用",
            )

    async def stop(self) -> None:
        if not self.started:
            return
        self.logger.info("正在关闭 FLLBot …")
        self.started = False
        try:
            await self.manager.stop()
        except Exception:
            self.logger.exception("关闭连接失败")
        try:
            await self.plugins.shutdown()
        except Exception:
            self.logger.exception("关闭插件失败")
        try:
            await self.dispatcher.shutdown()
            await self.dispatcher.drain()
        except Exception:
            self.logger.exception("关闭分发器失败")
        try:
            await self.chat.close()
        except Exception:
            self.logger.exception("关闭 LLM 客户端失败")
        try:
            await self.storage.close()
        except Exception:
            self.logger.exception("关闭数据库失败")
        self.logger.info("FLLBot 已关闭")

    # -- 事件入口 --------------------------------------------------------- #
    async def on_raw_event(self, connection: OneBotConnection, payload: dict[str, Any]) -> None:
        try:
            event = parse_event(payload)
        except Exception:
            self.logger.exception("事件解析失败")
            return

        if isinstance(event, MetaEvent):
            if event.meta_event_type == "lifecycle":
                self.logger.info("NapCat 生命周期：%s", event.sub_type or "connect")
            self.hub.publish({"type": "event", **event.to_summary()})
            self.dispatcher.emit(event, connection)
            return

        if isinstance(event, MessageEvent):
            try:
                await self.storage.record_message(
                    self_id=event.self_id,
                    scene=event.scene,
                    scene_id=event.scene_id,
                    user_id=event.user_id,
                    content=event.readable,
                )
            except Exception:
                self.logger.debug("记录消息失败", exc_info=True)

        self.hub.publish({"type": "event", **event.to_summary()})
        self.dispatcher.emit(event, connection)

    # -- 观测 ------------------------------------------------------------- #
    async def status(self) -> dict[str, Any]:
        uptime = time.time() - self.started_at if self.started_at else 0.0
        return {
            "version": __version__,
            "started": self.started,
            "uptime_seconds": round(uptime, 1),
            "started_at": _iso(self.started_at) if self.started_at else None,
            "root": str(self.root),
            "ws_path": self.config.server.ws_path,
            "connections": self.manager.summaries(),
            "plugins": self.plugin_summaries(),
            "failed_plugins": dict(self.plugins.failed),
            "dispatcher": self.dispatcher.summary(),
            "storage": await self.storage.summary(),
            "llm": self.chat.status(),
            "personas": self.personas.names(),
            "subscribers": self.hub.subscriber_count,
        }

    def plugin_summaries(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for plugin in self.plugins.loaded():
            entry = self.plugins.entry_for(plugin.name)
            metadata = plugin.metadata()
            metadata.update(
                {
                    "enabled": self.permissions.plugin_enabled(plugin.name, None),
                    "global_state": self.permissions.plugin_state(plugin.name, None),
                    "builtin": bool(entry.builtin) if entry else False,
                    "module": entry.module_name if entry else "",
                }
            )
            result.append(metadata)
        return result

    def safe_config(self) -> dict[str, Any]:
        return mask_secrets(self.config)

    # -- 内部 ------------------------------------------------------------- #
    def _ensure_admin_token(self) -> None:
        config = self.config.admin
        if config.token:
            self.admin_token = config.token
            return
        path = resolve_path(self.root, config.token_file)
        token = ""
        try:
            if path.is_file():
                token = path.read_text(encoding="utf-8").strip()
            if not token:
                token = secrets.token_urlsafe(24)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(token, encoding="utf-8")
        except OSError:
            self.logger.exception("管理令牌文件读写失败，改用内存临时令牌")
            token = token or secrets.token_urlsafe(24)
        self.admin_token = token
        self.logger.warning("管理 API 令牌：%s（文件：%s）", token, path)


def create_app(bot: FLLBot) -> FastAPI:
    """构造 FastAPI 应用（WebSocket + 管理 API）。"""
    from .api.routes import create_api_router

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await bot.start()
        try:
            yield
        finally:
            await bot.stop()

    app = FastAPI(
        title="FLLBot",
        version=__version__,
        description="NapCat / OneBot 11 backend with plugins, permissions and LLM chat",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    origins = list(bot.config.server.cors_origins or ["*"])
    allow_all = "*" in origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=None if allow_all else None,
        allow_credentials=not allow_all,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(create_api_router(bot))
    app.include_router(create_ws_router(bot))
    return app


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


__all__ = ["FLLBot", "create_app"]
