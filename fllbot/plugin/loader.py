"""插件加载器：目录扫描、动态导入、热重载、周期任务管理。"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from .base import LoopSpec, Plugin

if TYPE_CHECKING:  # pragma: no cover
    from ..app import FLLBot

USER_PACKAGE = "fllbot_user_plugins"
BUILTIN_PACKAGE = "fllbot.builtin_plugins"

_SAFE_NAME = re.compile(r"[^0-9A-Za-z_]")


@dataclass
class LoadedPlugin:
    """已加载插件的运行态。"""

    plugin: Plugin
    module_name: str
    source: Path
    builtin: bool
    mtime: float
    loops: list[asyncio.Task[Any]] = field(default_factory=list)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LoadedPlugin {self.plugin.name!r} from {self.source}>"


class PluginLoader:
    """扫描 ``plugins/`` 与内置插件包并维护其生命周期。"""

    def __init__(
        self,
        bot: FLLBot,
        dirs: Iterable[Path],
        *,
        load_builtin: bool = True,
        hot_reload: bool = True,
        watch_interval: float = 2.0,
    ) -> None:
        self.bot = bot
        self.dirs = [Path(item) for item in dirs]
        self.load_builtin = load_builtin
        self.hot_reload = hot_reload
        self.watch_interval = max(0.5, float(watch_interval))
        self.logger = logging.getLogger("fllbot.plugin.loader")

        self._loaded: dict[str, LoadedPlugin] = {}
        self._lock = asyncio.Lock()
        self._watch_task: asyncio.Task[None] | None = None
        self._known_mtimes: dict[Path, float] = {}
        self.failed: dict[str, str] = {}

    # -- 查询 ------------------------------------------------------------- #
    def get(self, name: str) -> Plugin | None:
        entry = self._loaded.get(name)
        return entry.plugin if entry else None

    def loaded(self) -> list[Plugin]:
        return [entry.plugin for entry in self._loaded.values()]

    def entry_for(self, name: str) -> LoadedPlugin | None:
        return self._loaded.get(name)

    def names(self) -> list[str]:
        return sorted(self._loaded)

    # -- 加载 ------------------------------------------------------------- #
    async def load_all(self) -> None:
        for directory in self.dirs:
            directory.mkdir(parents=True, exist_ok=True)
            for path in self._iter_candidates(directory):
                try:
                    await self.load_path(path)
                except Exception as exc:
                    self.logger.exception("插件加载失败：%s", path)
                    self.failed[str(path)] = str(exc)
        if self.load_builtin:
            await self._load_builtin()

    async def _load_builtin(self) -> None:
        try:
            package = importlib.import_module(BUILTIN_PACKAGE)
        except ImportError:  # pragma: no cover
            return
        base = Path(package.__file__).parent if package.__file__ else None
        if base is None:  # pragma: no cover
            return
        for path in sorted(item for item in base.iterdir() if item.is_dir()):
            if path.name.startswith("_") or not (path / "__init__.py").is_file():
                continue
            if not (path / "plugin.py").is_file() and not self._has_plugin_attribute(path / "__init__.py"):
                continue
            try:
                await self.load_path(path, builtin=True)
            except Exception:
                self.logger.exception("内置插件加载失败：%s", path.name)

    async def load_path(self, path: Path, *, builtin: bool = False) -> Plugin | None:
        async with self._lock:
            return await self._load_locked(Path(path), builtin=builtin)

    async def _load_locked(self, path: Path, *, builtin: bool) -> Plugin | None:
        module_name = self._module_name(path, builtin)
        module = self._import(path, module_name, builtin=builtin)
        plugin = getattr(module, "plugin", None)
        if not isinstance(plugin, Plugin):
            raise TypeError(f"{path} 没有导出 plugin = Plugin(...) 实例")

        if not plugin.name:
            plugin.name = path.stem if path.is_file() else path.name
            plugin.logger = logging.getLogger(f"fllbot.plugin.{plugin.name}")
            for spec in plugin.handlers:
                spec.plugin_name = plugin.name

        existing = self._loaded.get(plugin.name)
        if existing is not None:
            self.logger.info("插件 %s 已存在，先卸载旧版本", plugin.name)
            await self._unload_locked(plugin.name)

        plugin.bot = self.bot
        plugin.module = module
        plugin.path = path
        if not plugin.readme:
            plugin.readme = self._read_readme(path)

        for spec in plugin.handlers:
            self.bot.dispatcher.register(spec)

        try:
            await plugin.settings.load(self.bot.storage)
        except Exception:
            self.logger.exception("插件 %s 的配置加载失败", plugin.name)

        try:
            await plugin.on_load(self.bot)
        except Exception:
            self.logger.exception("插件 %s 的 on_load 失败", plugin.name)

        entry = LoadedPlugin(
            plugin=plugin,
            module_name=module_name,
            source=path,
            builtin=builtin,
            mtime=self._mtime(path),
        )
        entry.loops = self._start_loops(plugin)
        self._loaded[plugin.name] = entry
        self.failed.pop(str(path), None)
        self.logger.info(
            "已加载插件 %s v%s（%d 个处理器，来源 %s）",
            plugin.name,
            plugin.version,
            len(plugin.handlers),
            "内置" if builtin else path,
        )
        return plugin

    def _start_loops(self, plugin: Plugin) -> list[asyncio.Task[Any]]:
        tasks: list[asyncio.Task[Any]] = []
        for spec in plugin.loops:
            task = asyncio.create_task(self._loop_runner(plugin, spec), name=f"loop:{plugin.name}:{spec.name}")
            tasks.append(task)
        return tasks

    async def _loop_runner(self, plugin: Plugin, spec: LoopSpec) -> None:
        try:
            if spec.initial_delay > 0:
                await asyncio.sleep(spec.initial_delay)
            while True:
                try:
                    result = spec.callback(self.bot)
                    if asyncio.iscoroutine(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("周期任务异常：%s:%s", plugin.name, spec.name)
                await asyncio.sleep(spec.seconds)
        except asyncio.CancelledError:
            pass

    # -- 卸载 ------------------------------------------------------------- #
    async def unload(self, name: str) -> bool:
        async with self._lock:
            return await self._unload_locked(name)

    async def _unload_locked(self, name: str) -> bool:
        entry = self._loaded.pop(name, None)
        if entry is None:
            return False
        for task in entry.loops:
            task.cancel()
        if entry.loops:
            await asyncio.gather(*entry.loops, return_exceptions=True)
        self.bot.dispatcher.unregister_plugin(name)
        try:
            await entry.plugin.on_unload(self.bot)
        except Exception:
            self.logger.exception("插件 %s 的 on_unload 失败", name)
        self._purge_modules(entry.module_name)
        self.logger.info("已卸载插件 %s", name)
        return True

    async def reload(self, name: str) -> Plugin | None:
        async with self._lock:
            entry = self._loaded.get(name)
            if entry is None:
                raise KeyError(f"插件未加载: {name}")
            source, builtin = entry.source, entry.builtin
            await self._unload_locked(name)
            return await self._load_locked(source, builtin=builtin)

    async def reload_all(self) -> None:
        for name in list(self._loaded):
            try:
                await self.reload(name)
            except Exception:
                self.logger.exception("重载插件失败：%s", name)

    async def shutdown(self) -> None:
        await self.stop_watcher()
        for name in list(self._loaded):
            await self.unload(name)

    # -- 热重载 ----------------------------------------------------------- #
    def start_watcher(self) -> None:
        if not self.hot_reload or self._watch_task is not None:
            return
        self._watch_task = asyncio.create_task(self._watch_loop(), name="fllbot-plugin-watcher")

    async def stop_watcher(self) -> None:
        if self._watch_task is None:
            return
        self._watch_task.cancel()
        try:
            await self._watch_task
        except asyncio.CancelledError:
            pass
        self._watch_task = None

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(self.watch_interval)
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("插件热重载扫描失败")

    async def scan_once(self) -> list[str]:
        """扫描一次文件变化，返回发生变化的插件名。"""
        changed: list[str] = []
        current: dict[Path, float] = {}
        for directory in self.dirs:
            if not directory.is_dir():
                continue
            for path in self._iter_candidates(directory):
                current[path] = self._mtime(path)

        known_sources = {entry.source: entry for entry in self._loaded.values() if not entry.builtin}

        for path, mtime in current.items():
            previous = self._known_mtimes.get(path)
            entry = known_sources.get(path)
            if previous is None and entry is None:
                if await self._try_load(path):
                    changed.append(path.stem)
            elif entry is not None and previous is not None and mtime > previous:
                try:
                    plugin = await self.reload(entry.plugin.name)
                    if plugin:
                        changed.append(plugin.name)
                except Exception:
                    self.logger.exception("热重载失败：%s", path)

        for path, entry in known_sources.items():
            if path not in current:
                await self.unload(entry.plugin.name)
                changed.append(entry.plugin.name)

        self._known_mtimes = current
        return changed

    async def _try_load(self, path: Path) -> bool:
        try:
            plugin = await self.load_path(path)
            return plugin is not None
        except Exception as exc:
            self.logger.error("新插件加载失败 %s: %s", path, exc)
            self.failed[str(path)] = str(exc)
            return False

    # -- 工具 ------------------------------------------------------------- #
    @staticmethod
    def _has_plugin_attribute(path: Path) -> bool:  # pragma: no cover - 辅助判断
        try:
            return "plugin" in path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False

    @staticmethod
    def _read_readme(path: Path) -> str:
        """读取插件说明：目录插件用 ``README.md``，单文件插件用同名 ``.md``。"""
        max_chars = 64 * 1024
        candidates: list[Path] = []
        if path.is_dir():
            candidates = [path / "README.md", path / "readme.md"]
        elif path.is_file() and path.suffix == ".py":
            candidates = [path.with_suffix(".md")]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate.read_text(encoding="utf-8", errors="replace")[:max_chars]
            except OSError:
                continue
        return ""

    @staticmethod
    def _iter_candidates(directory: Path) -> list[Path]:
        candidates: list[Path] = []
        for item in sorted(directory.iterdir()):
            if item.name.startswith("_") or item.name.startswith("."):
                continue
            if item.is_file() and item.suffix == ".py":
                candidates.append(item)
            elif item.is_dir() and (item / "__init__.py").is_file():
                candidates.append(item)
        return candidates

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            if path.is_dir():
                return max(
                    (child.stat().st_mtime for child in path.rglob("*.py")),
                    default=path.stat().st_mtime,
                )
            return path.stat().st_mtime
        except OSError:
            return 0.0

    @staticmethod
    def _module_name(path: Path, builtin: bool) -> str:
        stem = path.name if path.is_dir() else path.stem
        safe = _SAFE_NAME.sub("_", stem)
        return f"{BUILTIN_PACKAGE}.{safe}" if builtin else f"{USER_PACKAGE}.{safe}"

    def _import(self, path: Path, module_name: str, *, builtin: bool) -> ModuleType:
        if builtin:
            sys.modules.pop(module_name, None)
            importlib.invalidate_caches()
            return importlib.import_module(module_name)

        # 直接编译源码执行：避免 __pycache__ 里同秒内写入的旧字节码被复用。
        self._purge_modules(module_name)
        self._ensure_parent_package()
        is_package = path.is_dir()
        location = (path / "__init__.py") if is_package else path
        source = location.read_text(encoding="utf-8")

        module = ModuleType(module_name)
        module.__file__ = str(location)
        module.__package__ = module_name if is_package else module_name.rpartition(".")[0]
        if is_package:
            module.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[module_name] = module
        try:
            code = compile(source, str(location), "exec")
            exec(code, module.__dict__)  # noqa: S102 - 插件源码来自本地可信目录
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module

    @staticmethod
    def _purge_modules(module_name: str) -> None:
        for key in [key for key in sys.modules if key == module_name or key.startswith(module_name + ".")]:
            sys.modules.pop(key, None)

    @staticmethod
    def _ensure_parent_package() -> None:
        if USER_PACKAGE in sys.modules:
            return
        package = ModuleType(USER_PACKAGE)
        package.__path__ = []  # type: ignore[attr-defined]
        sys.modules[USER_PACKAGE] = package


__all__ = ["LoadedPlugin", "PluginLoader"]
