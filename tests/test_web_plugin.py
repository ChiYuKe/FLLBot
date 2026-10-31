"""联网工具插件测试（httpx.MockTransport，不访问网络）。"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest

from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot

ROOT = Path(__file__).resolve().parents[1]


def load_web_plugin() -> Any:
    spec = importlib.util.spec_from_file_location("fllbot_test_web_plugin", ROOT / "plugins" / "web" / "plugin.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


web = load_web_plugin()

BING_HTML = """
<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="https://example.com/one">武汉天气预报</a></h2>
  <div class="b_caption"><p>武汉今天 18°C，多云。</p></div></li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS90d28">第二个结果</a></h2>
  <div class="b_caption"><p>带跳转链接的摘要&nbsp;内容。</p></div></li>
</ol></body></html>
"""

PLACE = {
    "results": [
        {
            "name": "武汉",
            "latitude": 30.5833,
            "longitude": 114.2667,
            "country": "中国",
            "admin1": "湖北省",
            "timezone": "Asia/Shanghai",
        }
    ]
}

FORECAST = {
    "timezone": "Asia/Shanghai",
    "utc_offset_seconds": 28800,
    "current": {
        "time": "2026-10-31T05:45",
        "temperature_2m": 18.4,
        "apparent_temperature": 17.9,
        "relative_humidity_2m": 65,
        "precipitation": 0.0,
        "weather_code": 2,
        "wind_speed_10m": 7.2,
        "is_day": 0,
    },
    "daily": {
        "time": ["2026-10-31", "2026-11-01"],
        "weather_code": [2, 61],
        "temperature_2m_max": [21.3, 19.8],
        "temperature_2m_min": [12.1, 13.4],
        "precipitation_probability_max": [10, 70],
    },
}


def use_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    def _new_client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(web, "_new_client", _new_client)


def test_parse_bing_extracts_results_and_expands_redirect() -> None:
    results = web.parse_bing(BING_HTML, limit=5)
    assert [item["title"] for item in results] == ["武汉天气预报", "第二个结果"]
    assert results[0]["url"] == "https://example.com/one"
    assert results[0]["snippet"] == "武汉今天 18°C，多云。"
    assert results[1]["url"] == "https://example.com/two"
    assert results[1]["snippet"] == "带跳转链接的摘要 内容。"


def test_parse_bing_returns_empty_without_results() -> None:
    assert web.parse_bing("<html><body>nothing</body></html>") == []


async def test_run_search_bing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200, text=BING_HTML, headers={"content-type": "text/html; charset=utf-8"})

    use_transport(monkeypatch, handler)
    data = await web.run_search("武汉天气", count=5)

    assert seen == ["cn.bing.com"]
    assert data["query"] == "武汉天气"
    assert data["backend"] == "bing"
    assert len(data["results"]) == 2
    assert "error" not in data


async def test_run_search_reports_missing_key() -> None:
    data = await web.run_search("test", backend="tavily", tavily_key="")
    assert "Tavily API Key" in data["error"]


async def test_run_search_reports_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    use_transport(monkeypatch, handler)
    data = await web.run_search("test")
    assert "联网搜索失败" in data["error"]


async def test_run_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=PLACE)
        assert request.url.host == "api.open-meteo.com"
        assert request.url.params["forecast_days"] == "2"
        return httpx.Response(200, json=FORECAST)

    use_transport(monkeypatch, handler)
    data = await web.run_weather("武汉市", days=2)

    assert data["city"] == "武汉"
    assert data["region"] == "湖北省，中国"
    assert data["utc_offset"] == "UTC+08:00"
    assert data["current"]["weather"] == "多云"
    assert data["current"]["temperature_c"] == 18.4
    assert [item["weekday"] for item in data["daily"]] == ["星期六", "星期日"]
    assert data["daily"][1]["weather"] == "小雨"
    assert data["daily"][1]["precip_prob_percent"] == 70

    text = web.format_weather(data)
    assert "武汉（湖北省，中国）" in text
    assert "多云" in text
    assert "12.1~21.3°C" in text


async def test_run_weather_unknown_city(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    use_transport(monkeypatch, handler)
    data = await web.run_weather("不存在的地方")
    assert "找不到城市" in data["error"]


async def test_run_time_local() -> None:
    data = await web.run_time()
    assert data["weekday"].startswith("星期")
    assert len(data["date"]) == 10
    assert "现在是" in web.format_time(data)


async def test_run_time_for_city(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=PLACE)
        return httpx.Response(200, json=FORECAST)

    use_transport(monkeypatch, handler)
    data = await web.run_time("武汉")
    assert data["city"] == "武汉"
    assert data["time"] == "2026-10-31 05:45"
    assert data["weekday"] == "星期六"
    assert data["utc_offset"] == "UTC+08:00"


def test_plugin_registers_tools() -> None:
    names = {spec.name for spec in web.plugin.ai_tools}
    assert names == {"web_search", "read_webpage", "get_weather", "get_current_time"}
    for spec in web.plugin.ai_tools:
        assert spec.plugin_name == "web"
        assert spec.parameters["type"] == "object"


def test_format_search_limits_and_reports_errors() -> None:
    data = {"results": [{"title": "标题", "url": "https://example.com", "snippet": "摘要"}]}
    text = web.format_search(data, limit=5)
    assert "1. 标题" in text and "https://example.com" in text
    assert web.format_search({"error": "失败"}) == "失败"
    assert web.format_search({"results": []}) == "没有找到相关结果。"


def test_real_url_keeps_plain_links() -> None:
    assert web._real_url("https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert urlsplit(web._real_url("https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbQ")).hostname == "example.com"


async def test_agent_only_sees_selected_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "web", plugin_dir / "web")

    bot = await make_bot(
        tmp_path,
        llm={"enabled": True, "api_key": "fake-test-key"},
        plugin_dirs=[plugin_dir],
    )
    try:
        agent = bot.plugins.get("agent")
        assert agent is not None
        await agent.settings.update(
            {"auto_group": True, "trigger_mode": "all", "cooldown_seconds": 0,
             "allowed_tools": ["web__get_weather"]}
        )
        complete = AsyncMock(return_value=SimpleNamespace(text="收到", tool_calls=[]))
        monkeypatch.setattr(bot.chat, "complete", complete)

        await bot.dispatcher.dispatch(parse_event(group_message("武汉现在天气怎么样")), FakeConnection())

        tools = complete.call_args.kwargs["tools"]
        assert {item["function"]["name"] for item in tools} == {"web__get_weather"}
    finally:
        await bot.stop()


async def test_tool_titles_are_exposed_to_console(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "web", plugin_dir / "web")

    bot = await make_bot(
        tmp_path,
        llm={"enabled": True, "api_key": "fake-test-key"},
        plugin_dirs=[plugin_dir],
    )
    try:
        web = bot.plugins.get("web")
        assert web is not None
        summaries = {item["name"]: item for item in bot.plugin_summaries()}
        titles = {tool["name"]: tool["title"] for tool in summaries["web"]["ai_tools"]}
        assert titles == {
            "web__web_search": "联网搜索",
            "web__read_webpage": "读取网页",
            "web__get_weather": "天气查询",
            "web__get_current_time": "当前时间",
        }
    finally:
        await bot.stop()


async def test_agent_sees_all_granted_web_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "web", plugin_dir / "web")

    bot = await make_bot(
        tmp_path,
        llm={"enabled": True, "api_key": "fake-test-key"},
        plugin_dirs=[plugin_dir],
    )
    try:
        agent = bot.plugins.get("agent")
        assert agent is not None
        await agent.settings.update(
            {"auto_group": True, "trigger_mode": "all", "cooldown_seconds": 0,
             "allowed_tools": ["web__web_search", "web__get_weather", "web__get_current_time"]}
        )
        complete = AsyncMock(return_value=SimpleNamespace(text="收到", tool_calls=[]))
        monkeypatch.setattr(bot.chat, "complete", complete)

        await bot.dispatcher.dispatch(parse_event(group_message("武汉现在天气怎么样")), FakeConnection())

        tools = complete.call_args.kwargs["tools"]
        assert {item["function"]["name"] for item in tools} == {
            "web__web_search",
            "web__get_weather",
            "web__get_current_time",
        }
    finally:
        await bot.stop()
