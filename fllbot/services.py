"""On-demand external service checks declared by plugins; GET only returns cached state."""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import FLLBot


class ServiceCheckBusyError(RuntimeError):
    pass


class ServiceMonitor:
    def __init__(self, bot: FLLBot) -> None:
        self.bot = bot
        self.results: dict[str, tuple[Any, dict[str, Any]]] = {}
        self.busy = False
        self.running: set[str] = set()

    def catalog(self) -> dict[str, tuple[Any, Any]]:
        return {f'{plugin.name}__{spec.name}': (plugin, spec)
                for plugin in self.bot.plugins.loaded() for spec in plugin.service_checks}

    def snapshot(self) -> dict[str, Any]:
        catalog = self.catalog()
        for key in list(self.results):
            if key not in catalog or self.results[key][0] is not catalog[key][0]:
                self.results.pop(key)
        items = []
        for key, (plugin, spec) in catalog.items():
            result = self.results.get(key)
            selected = spec.selected_when is None or plugin.settings.get(spec.selected_when[0]) == spec.selected_when[1]
            cached = result[1] if result else {'state': 'unknown', 'code': 'not_checked', 'checked_at': None, 'latency_ms': None}
            items.append({'id': key, 'title': spec.title, 'plugin': plugin.name, 'selected': selected,
                          'plugin_enabled': self.bot.permissions.plugin_enabled(plugin.name, None),
                          **cached, 'checking': key in self.running,
                          'stale': bool(cached['checked_at'] and time.time() - cached['checked_at'] > 60)})
        return {'items': items, 'checking': self.busy, 'note': '仅显示插件声明的检查。读取页面不联网；点击检查只调用健康接口，不生成内容或启动服务。'}

    async def check(self, service_id: str | None = None) -> dict[str, Any]:
        if self.busy:
            raise ServiceCheckBusyError('已有服务检查正在执行')
        catalog = self.catalog()
        if service_id is not None:
            if service_id not in catalog:
                raise KeyError(service_id)
            catalog = {service_id: catalog[service_id]}
        self.busy = True
        semaphore = asyncio.Semaphore(4)

        async def run(key: str, plugin: Any, spec: Any) -> None:
            async with semaphore:
                self.running.add(key)
                started = time.perf_counter()
                try:
                    result = await asyncio.wait_for(spec.callback(), timeout=spec.timeout)
                    if not isinstance(result, dict) or result.get('state') not in {'ready', 'loading', 'idle', 'unavailable', 'error'}:
                        result = {'state': 'error', 'code': 'invalid_response'}
                    code = result.get('code', 'checked')
                    if not isinstance(code, str) or not re.fullmatch(r'[a-z_]{1,64}', code):
                        code = 'checked'
                    row = {'state': result['state'], 'code': code}
                except TimeoutError:
                    row = {'state': 'unavailable', 'code': 'timeout'}
                except Exception:
                    row = {'state': 'error', 'code': 'check_failed'}
                finally:
                    self.running.discard(key)
                self.results[key] = (plugin, {**row, 'checked_at': time.time(),
                                              'latency_ms': round((time.perf_counter() - started) * 1000, 2)})
        try:
            await asyncio.gather(*(run(key, plugin, spec) for key, (plugin, spec) in catalog.items()))
        finally:
            self.busy = False
        return self.snapshot()
