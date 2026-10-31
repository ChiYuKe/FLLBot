"""插件加载器：目录扫描、动态导入、热重载、周期任务管理。"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import LoopSpec, Plugin
from .imports import BUILTIN_PACKAGE, import_plugin, module_name_for, purge_modules
from .manifest import PluginManifest

if TYPE_CHECKING:  # pragma: no cover
    from ..app import FLLBot


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
        self._known_fingerprints: dict[Path, tuple[Any, ...]] = {}
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
            except Exception as exc:
                self.failed[str(path.resolve())] = str(exc)
                self.logger.exception("内置插件加载失败：%s", path.name)

    async def load_path(self, path: Path, *, builtin: bool = False) -> Plugin | None:
        async with self._lock:
            path = Path(path).resolve()
            try:
                return await self._load_locked(path, builtin=builtin)
            except Exception as exc:
                self.failed[str(path)] = str(exc)
                raise

    async def _load_locked(self, path: Path, *, builtin: bool, replace: str | None = None) -> Plugin | None:
        manifest = PluginManifest.read(path) if path.is_dir() and (path / "plugin.toml").exists() else None
        module_name = module_name_for(path, builtin)
        backup = {
            key: value
            for key, value in list(sys.modules.items())
            if key == module_name or key.startswith(module_name + ".")
        }
        plugin = None
        initializing = False
        try:
            module = import_plugin(path, module_name, builtin=builtin)
            plugin = getattr(module, "plugin", None)
            if not isinstance(plugin, Plugin):
                raise TypeError(f"{path} 没有导出 plugin = Plugin(...) 实例")
            if not plugin.name:
                plugin.name = path.stem if path.is_file() else path.name
                plugin.logger = logging.getLogger(f"fllbot.plugin.{plugin.name}")
                for spec in plugin.handlers:
                    spec.plugin_name = plugin.name
            if manifest:
                for key in ("name", "version", "description", "author", "category"):
                    if getattr(plugin, key) != getattr(manifest, key):
                        raise ValueError(f"插件 {key} 与 plugin.toml 不一致，请使用 Plugin.from_manifest(__file__)")
            existing = self._loaded.get(plugin.name)
            if existing is not None and existing.source != path:
                raise ValueError(f"插件名重复：{plugin.name} 已由 {existing.source} 提供")
            plugin.bot, plugin.module, plugin.path = self.bot, module, path
            if not plugin.readme:
                plugin.readme = self._read_readme(path)
            await plugin.settings.load(self.bot.storage)
            # Stage initialization before replacing the working version or exposing handlers.
            initializing = True
            await plugin.on_load(self.bot)
        except BaseException:
            if isinstance(plugin, Plugin):
                if initializing:
                    try:
                        await plugin.on_unload(self.bot)
                    except Exception:
                        self.logger.exception("失败插件的卸载回调异常：%s", path)
                await plugin.cleanup()
            purge_modules(module_name)
            sys.modules.update(backup)
            raise

        # 新源码已经导入并校验成功，这时才卸载旧版本：
        # 新版本有语法/导入错误时，正在运行的旧插件不受影响。
        staged_modules = {
            key: value
            for key, value in list(sys.modules.items())
            if key == module_name or key.startswith(module_name + ".")
        }
        for stale_name in dict.fromkeys((plugin.name, replace)):
            if stale_name is None:
                continue
            existing = self._loaded.get(stale_name)
            if existing is None:
                continue
            self.logger.info("插件 %s 已存在，先卸载旧版本", stale_name)
            # 新版本已经占用了同一个模块名，此时清模块会把刚导入的新模块也删掉。
            if existing.module_name == module_name:
                # Old cleanup hooks must see their own package during lazy relative imports.
                purge_modules(module_name)
                sys.modules.update(backup)
                try:
                    await self._unload_locked(stale_name, purge=False)
                finally:
                    purge_modules(module_name)
                    sys.modules.update(staged_modules)
            else:
                await self._unload_locked(stale_name)

        for spec in plugin.handlers:
            self.bot.dispatcher.register(spec)

        entry = LoadedPlugin(
            plugin=plugin,
            module_name=module_name,
            source=path,
            builtin=builtin,
            mtime=self._mtime(path),
        )
        entry.loops = self._start_loops(plugin)
        self._loaded[plugin.name] = entry
        self._known_fingerprints[path] = self._fingerprint(path)
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

    async def _unload_locked(self, name: str, *, purge: bool = True) -> bool:
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
        await entry.plugin.cleanup()
        if purge:
            purge_modules(entry.module_name)
        self.logger.info("已卸载插件 %s", name)
        return True

    async def reload(self, name: str) -> Plugin | None:
        async with self._lock:
            entry = self._loaded.get(name)
            if entry is None:
                raise KeyError(f"插件未加载: {name}")
            source, builtin = entry.source, entry.builtin
            # 先导入新版本，成功后再由 _load_locked 卸载旧版本；
            # 新版本有语法/导入错误时抛出异常，旧插件继续服务。
            try:
                return await self._load_locked(source, builtin=builtin, replace=name)
            except Exception as exc:
                self.failed[str(source)] = str(exc)
                raise

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
        current: dict[Path, tuple[Any, ...]] = {}
        for directory in self.dirs:
            if not directory.is_dir():
                continue
            for path in self._iter_candidates(directory):
                path = path.resolve()
                current[path] = self._fingerprint(path)

        known_sources = {entry.source: entry for entry in self._loaded.values() if not entry.builtin}

        for path, fingerprint in current.items():
            previous = self._known_fingerprints.get(path)
            entry = known_sources.get(path)
            if entry is not None:
                if previous is not None and fingerprint != previous:
                    try:
                        plugin = await self.reload(entry.plugin.name)
                        if plugin:
                            changed.append(plugin.name)
                    except Exception:
                        self.logger.exception("热重载失败：%s", path)
            elif previous is None or fingerprint != previous:
                # 新插件，或此前加载/重载失败（源码有错）而现在源码又被改动的插件：
                # 重新尝试加载，避免一次坏保存让插件在进程内永久消失。
                plugin = await self._try_load(path)
                if plugin is not None:
                    changed.append(plugin.name)

        for path, entry in known_sources.items():
            if path not in current:
                await self.unload(entry.plugin.name)
                changed.append(entry.plugin.name)

        for failed_path in list(self.failed):
            if not Path(failed_path).exists():
                self.failed.pop(failed_path, None)
        self._known_fingerprints = current
        return changed

    async def _try_load(self, path: Path) -> Plugin | None:
        try:
            return await self.load_path(path)
        except Exception as exc:
            self.logger.error("插件加载失败 %s: %s", path, exc)
            self.failed[str(path)] = str(exc)
            return None

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
            elif item.is_dir() and ((item / "__init__.py").is_file() or (item / "plugin.toml").is_file()):
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
    def _fingerprint(path: Path) -> tuple[Any, ...]:
        """Track additions, removals and changes across the package, not only max mtime."""
        candidates = path.rglob("*") if path.is_dir() else [path, path.with_suffix(".md")]
        result = []
        for child in candidates:
            if child.suffix.lower() not in {".py", ".md", ".toml", ".json", ".yaml", ".yml"}:
                continue
            if any(part.startswith(".") or part == "__pycache__" for part in child.relative_to(path.parent).parts):
                continue
            try:
                if child.is_file():
                    stat = child.stat()
                    result.append((str(child.relative_to(path.parent)), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        return tuple(sorted(result))


__all__ = ["LoadedPlugin", "PluginLoader"]
