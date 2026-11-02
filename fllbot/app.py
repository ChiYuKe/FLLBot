"""应用装配：持久化、连接管理、事件分发、插件、LLM 与管理 API 的组装与生命周期。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from . import __version__
from .bots import BotProfiles
from .config import AppConfig, ConfigStore, mask_secrets, resolve_path, split_startup_overrides
from .dispatcher import Dispatcher
from .events import DedupCache, EventHub
from .llm.chat import ChatService
from .llm.personas import PersonaStore
from .logging_setup import configure_logging
from .maintenance import BackupManager
from .onebot.connection import OneBotConnection
from .onebot.models import MessageEvent, MetaEvent, NoticeEvent, parse_event
from .onebot.server import ConnectionManager, create_ws_router
from .permission import PermissionManager
from .plugin.loader import PluginLoader
from .policy import migrate_tool_permissions
from .resources import ResourceReplacementManager
from .retention import CleanupManager
from .services import ServiceMonitor
from .storage import Storage
from .tracing import MessageTrace, TraceRecorder
from .webui import render_index


class FLLBot:
    """整个后端的根对象。"""

    def __init__(self, config_store: ConfigStore, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.config_store = config_store
        self.startup_values = config_store.freeze_startup_values()
        self.inactive_startup_overrides: dict[str, Any] = {}
        self.logger = logging.getLogger("fllbot")

        config = config_store.current
        self.storage = Storage(resolve_path(self.root, config.storage.path))
        self.traces = TraceRecorder(self.storage)
        self.backups = BackupManager(self)
        self.services = ServiceMonitor(self)
        self.cleanup = CleanupManager(self)
        self.replacements = ResourceReplacementManager(self)
        self.hub = EventHub()
        self.bots = BotProfiles(self)
        self._dedupe = DedupCache(size=1024, ttl=180.0)
        self.permissions = PermissionManager(self)
        self.dispatcher = Dispatcher(self)
        self.personas = PersonaStore(
            [self.root / "personas"],
            default_name=config.llm.persona,
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
        async with self.storage.settings_lock:
            config, overrides = self.config_store.prepare_patch(patch)
            transaction = asyncio.create_task(self.storage.meta_set("config_overrides", overrides))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(transaction)
                    break
                except asyncio.CancelledError:
                    if transaction.cancelled():
                        raise
                    cancelled = True
            # No await between publishing the validated snapshot and its runtime effects.
            self.config_store.restore(overrides)
            self._apply_runtime_config(config)
            if cancelled:
                raise asyncio.CancelledError
            self.logger.info("运行时配置已更新：%s", ", ".join(sorted(patch)))
            return config

    def config_revision(self) -> str:
        return hashlib.sha256(json.dumps(self.config.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _apply_runtime_config(self, config: AppConfig) -> None:
        if config.admin.token:
            self.admin_token = config.admin.token
        self.personas.default_name = config.llm.persona
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
            try:
                overrides = await self.storage.meta_get("config_overrides", {})
                self.inactive_startup_overrides = await self.storage.meta_get('inactive_startup_overrides', {})
                if isinstance(overrides, dict):
                    overrides, archived = split_startup_overrides(overrides)
                    if archived:
                        self.inactive_startup_overrides = await self.storage.archive_startup_overrides(overrides, archived)
                        self.logger.warning('旧版未生效的启动配置覆盖已保留到历史记录：%s', '、'.join(sorted(archived)))
                if isinstance(overrides, dict) and overrides:
                    self.config_store.restore(overrides)
                    self._apply_runtime_config(self.config)
                    self.logger.info("已恢复运行时的配置覆盖")
            except BaseException:
                await self.storage.close()
                raise

            if self.config.admin.enabled:
                self._ensure_admin_token()
            self.personas.load()
            await self.permissions.load()
            await self.bots.load()
            await self.plugins.load_all()
            await migrate_tool_permissions(self)
            self.manager.heartbeat_timeout = float(self.config.server.heartbeat_timeout or 0)
            await self.dispatcher.startup()
            self.dispatcher.start()
            self.traces.start()
            self.cleanup.start_scheduler()
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
        self.dispatcher.stop_accepting()
        try:
            await self.cleanup.close()
        except Exception:
            self.logger.exception("停止数据清理失败")
        try:
            await self.manager.stop()
        except Exception:
            self.logger.exception("关闭连接失败")
        try:
            await self.plugins.stop_background_tasks()
        except Exception:
            self.logger.exception("停止插件后台任务失败")
        try:
            await self.dispatcher.drain()
        except Exception:
            self.logger.exception("关闭分发器失败")
        try:
            await self.dispatcher.shutdown()
        except Exception:
            self.logger.exception("插件关闭钩子失败")
        try:
            await self.plugins.shutdown()
        except Exception:
            self.logger.exception("卸载插件失败")
        try:
            await self.chat.close()
        except Exception:
            self.logger.exception("关闭 LLM 客户端失败")
        try:
            await self.traces.close()
        except Exception:
            self.logger.exception("关闭消息诊断记录失败")
        try:
            await self.backups.close()
        except Exception:
            self.logger.exception("等待数据库备份结束失败")
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

        trace = MessageTrace(event, connection) if isinstance(event, MessageEvent) else None
        if trace:
            trace.add('receive', 'received')
        dedupe_key = _dedupe_key(event)
        if dedupe_key and self._dedupe.seen(dedupe_key):
            if trace:
                trace.add('dedup', 'duplicate_message', 'skipped')
                self.traces.submit(trace)
            self.logger.debug("忽略重复事件：%s", dedupe_key)
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
        self.dispatcher.emit(event, connection, trace=trace)

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
                    'revision': self.permissions.plugin_revision(plugin.name),
                    'blocked_by_global': self.permissions.plugin_state(plugin.name) is False,
                    "builtin": bool(entry.builtin) if entry else False,
                    "module": entry.module_name if entry else "",
                    "package": bool(entry and entry.source.is_dir()),
                    "manifest": bool(entry and entry.source.is_dir() and (entry.source / "plugin.toml").is_file()),
                    "folder": entry.source.name if entry and entry.source.is_dir() else "",
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
    from .api.console import create_console_router
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
        docs_url="/api/docs" if bot.config.admin.enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if bot.config.admin.enabled else None,
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
    app.include_router(create_console_router(bot))
    app.include_router(create_ws_router(bot))

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    async def webui() -> HTMLResponse:
        """自带控制台页面；数据仍走 /api/*，由页面自己带令牌。"""
        if not bot.config.admin.enabled:
            raise HTTPException(404, '管理入口已关闭')
        return HTMLResponse(
            render_index(),
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
            },
        )

    return app


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _dedupe_key(event: Any) -> str | None:
    """同一条消息 / 通知在重连后被重放时用于去重。"""
    if isinstance(event, MessageEvent) and event.message_id:
        return f"message:{event.self_id}:{event.message_id}"
    if isinstance(event, NoticeEvent) and event.notice_type:
        return (
            f"notice:{event.self_id}:{event.notice_type}:{event.sub_type}:"
            f"{event.group_id}:{event.user_id}:{event.time}"
        )
    return None


__all__ = ["FLLBot", "create_app"]
