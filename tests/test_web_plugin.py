"""联网工具插件测试（httpx.MockTransport，不访问网络）。"""

from __future__ import annotations

import importlib.util
import io
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest
from PIL import Image

from fllbot.onebot.models import parse_event
from fllbot.plugin import Context
from helpers import FakeConnection, group_message, make_bot

ROOT = Path(__file__).resolve().parents[1]


def load_web_plugin() -> Any:
    """按宿主的方式导入插件包，返回 plugin.py 模块。"""
    package_name = "fllbot_test_web_plugin"
    if package_name in sys.modules:
        return sys.modules[package_name + ".plugin"]
    directory = ROOT / "plugins" / "web"
    spec = importlib.util.spec_from_file_location(
        package_name, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    assert spec and spec.loader
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    return sys.modules[package_name + ".plugin"]


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

QQ_CITY = {"status": 200, "message": "ok", "data": {"101200101": "湖北, 武汉"}}

QQ_WEATHER = {
    "status": 200,
    "message": "OK",
    "data": {
        "observe": {
            "degree": "14",
            "humidity": "100",
            "precipitation": "1.4",
            "pressure": "1015",
            "update_time": "202611131530",
            "weather": "雨",
            "weather_code": "301",
            "weather_short": "雨",
            "wind_direction": "3",
            "wind_power": "3-4",
            "wind_direction_name": "东南风",
        },
        "forecast_24h": {
            "0": {
                "time": "2026-11-12",
                "day_weather": "晴",
                "day_weather_code": "00",
                "max_degree": "20",
                "min_degree": "12",
                "aqi": 60,
                "aqi_name": "良",
            },
            "1": {
                "time": "2026-11-13",
                "day_weather": "小雨",
                "day_weather_code": "07",
                "max_degree": "13",
                "min_degree": "11",
                "aqi": 45,
                "aqi_name": "优",
            },
            "2": {
                "time": "2026-11-14",
                "day_weather": "阴",
                "day_weather_code": "02",
                "max_degree": "19",
                "min_degree": "12",
                "aqi": 75,
                "aqi_name": "良",
            },
        },
        "air": {"aqi": 45, "aqi_name": "优"},
        "forecast_1h": {
            "0": {"update_time": "20261113150000", "degree": "14", "weather": "小雨", "weather_code": "07"},
            "1": {"update_time": "20261113160000", "degree": "14", "weather": "小雨", "weather_code": "07"},
        },
        "index": {
            "drying": {"name": "晾晒", "info": "不宜", "detail": "有降水，不适宜晾晒"},
            "sports": {"name": "运动", "info": "较不宜", "detail": "推荐室内运动"},
            "morning": {"name": "晨练", "info": "不宜", "detail": "避免户外晨练"},
            "clothes": {"name": "穿衣", "info": "较冷", "detail": "建议厚外套"},
            "cold": {"name": "感冒", "info": "较易发", "detail": "注意防护"},
        },
        "rise": {"0": {"time": "20261113", "sunrise": "06:47", "sunset": "17:28"}},
    },
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
    _patch_client(monkeypatch, web, handler)


def _patch_client(monkeypatch: pytest.MonkeyPatch, module: Any, handler: Any) -> None:
    def _new_client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(module, "_new_client", _new_client)


def forecast_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "geocoding-api.open-meteo.com":
        return httpx.Response(200, json=PLACE)
    assert request.url.host == "api.open-meteo.com"
    return httpx.Response(200, json=FORECAST)


def qq_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "wis.qq.com"
    if request.url.path == "/city/like":
        return httpx.Response(200, json=QQ_CITY)
    assert request.url.path == "/weather/common"
    return httpx.Response(200, json=QQ_WEATHER)


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


async def test_run_weather_open_meteo(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=PLACE)
        assert request.url.host == "api.open-meteo.com"
        assert request.url.params["forecast_days"] == "2"
        return httpx.Response(200, json=FORECAST)

    use_transport(monkeypatch, handler)
    data = await web.run_weather("武汉市", days=2, backend="open-meteo")

    assert data["city"] == "武汉"
    assert data["region"] == "湖北省，中国"
    assert data["utc_offset"] == "UTC+08:00"
    assert data["current"]["weather"] == "多云"
    assert data["current"]["code"] == 2
    assert data["current"]["temperature_c"] == 18.4
    assert [item["weekday"] for item in data["daily"]] == ["星期六", "星期日"]
    assert data["daily"][1]["weather"] == "小雨"
    assert data["daily"][1]["code"] == 61
    assert data["daily"][1]["precip_prob_percent"] == 70

    text = web.format_weather(data)
    assert "武汉（湖北省，中国）" in text
    assert "多云" in text
    assert "12.1~21.3°C" in text


async def test_run_weather_qq(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/city/like":
            seen.append(request.url.params["city"])
            return httpx.Response(200, json=QQ_CITY if request.url.params["city"] == "武汉" else {"data": {}})
        assert request.url.params["province"] == "湖北"
        assert request.url.params["city"] == "武汉"
        return httpx.Response(200, json=QQ_WEATHER)

    use_transport(monkeypatch, handler)
    data = await web.run_weather("武汉市", days=3)

    # 带“市”的输入会先原样查一次，再用去掉后缀的名字重试。
    assert seen == ["武汉市", "武汉"]
    assert data["city"] == "武汉"
    assert data["region"] == "湖北"
    assert data["source"] == "QQ 天气"
    assert data["observed_at"] == "2026-11-13T15:30"
    assert data["sunrise"] == "06:47"
    current = data["current"]
    assert current["weather"] == "雨"
    assert current["icon"] == "rain"
    assert current["temperature_c"] == 14.0
    assert current["humidity_percent"] == 100
    assert current["wind_text"] == "东南风 3-4级"
    assert current["wind_kmh"] is None
    assert current["apparent_c"] is None
    # 预报里的昨天会被过滤掉，只保留今天起的三天。
    assert [item["date"] for item in data["daily"]] == ["2026-11-13", "2026-11-14"]
    assert data["daily"][0]["icon"] == "rain"
    assert data["daily"][1]["icon"] == "overcast"
    assert data["daily"][0]["aqi_name"] == "优"
    assert data["air"] == {"aqi": 45, "name": "优"}
    # 逐小时预报只保留当前小时之后的时段并按时间排序，生活指数按卡片固定顺序取。
    times = [item["time"] for item in data["hourly"]]
    assert times and times == sorted(times)
    assert set(times) <= {"15:00", "16:00"}
    assert [item["name"] for item in data["index"]] == ["晾晒", "运动", "晨练", "穿衣", "感冒"]

    text = web.format_weather(data)
    assert "武汉（湖北）" in text
    assert "体感" not in text
    assert "东南风 3-4级" in text
    assert "空气质量：优（AQI 45）" in text
    assert "生活指数：晾晒 不宜" in text
    assert "降水概率" not in text


async def test_run_weather_qq_unknown_city(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 200, "message": "ok", "data": {}})

    use_transport(monkeypatch, handler)
    data = await web.run_weather("Tokyo")
    assert "只覆盖国内城市" in data["error"]


async def test_run_weather_unknown_city(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    use_transport(monkeypatch, handler)
    data = await web.run_weather("不存在的地方", backend="open-meteo")
    assert "找不到城市" in data["error"]


async def test_run_time_local() -> None:
    data = await web.run_time()
    assert data["weekday"].startswith("星期")
    assert len(data["date"]) == 10
    assert "现在是" in web.format_time(data)


async def test_run_time_qq_city(monkeypatch: pytest.MonkeyPatch) -> None:
    use_transport(monkeypatch, qq_handler)
    data = await web.run_time("武汉")
    assert data["city"] == "武汉"
    assert data["timezone"] == "Asia/Shanghai"
    assert data["utc_offset"] == "UTC+08:00"
    assert len(data["time"]) == 19


async def test_run_time_for_city(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=PLACE)
        return httpx.Response(200, json=FORECAST)

    use_transport(monkeypatch, handler)
    data = await web.run_time("武汉", backend="open-meteo")
    assert data["city"] == "武汉"
    assert data["time"] == "2026-10-31 05:45"
    assert data["weekday"] == "星期六"
    assert data["utc_offset"] == "UTC+08:00"


def test_plugin_registers_tools() -> None:
    names = {spec.name for spec in web.plugin.ai_tools}
    assert names == {"web_search", "read_webpage", "get_weather", "weather_card", "get_current_time"}
    for spec in web.plugin.ai_tools:
        assert spec.plugin_name == "web"
        assert spec.parameters["type"] == "object"


def weather_data() -> dict[str, Any]:
    return {
        "city": "武汉",
        "region": "湖北省，中国",
        "timezone": "Asia/Shanghai",
        "utc_offset": "UTC+08:00",
        "observed_at": "2026-10-31T05:45",
        "current": {
            "weather": "多云",
            "code": 2,
            "temperature_c": 18.4,
            "apparent_c": 17.9,
            "humidity_percent": 65,
            "precipitation_mm": 0.0,
            "wind_kmh": 7.2,
            "is_day": False,
        },
        "daily": [
            {"date": "2026-10-31", "weekday": "星期六", "weather": "多云", "code": 2,
             "temp_max_c": 21.3, "temp_min_c": 12.1, "precip_prob_percent": 10},
            {"date": "2026-11-01", "weekday": "星期日", "weather": "小雨", "code": 61,
             "temp_max_c": 19.8, "temp_min_c": 13.4, "precip_prob_percent": 70},
        ],
    }


def test_weather_card_renderer_outputs_png() -> None:
    image = web.render_weather_card(weather_data())
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(image)) as card:
        assert card.format == "PNG"
        assert card.width == 900
        assert card.height > 400


def test_weather_card_renderer_includes_hourly_and_indices() -> None:
    data = weather_data()
    data["hourly"] = [
        {"time": "16:00", "weather": "小雨", "degree_c": 14, "icon": "rain"},
        {"time": "17:00", "weather": "阴", "degree_c": 13, "icon": "overcast"},
    ]
    data["index"] = [
        {"key": "drying", "name": "晾晒", "info": "不宜", "detail": "有降水，不适宜晾晒。若需要晾晒，请在室内准备出充足的空间。"},
        {"key": "sports", "name": "运动", "info": "较不宜", "detail": "推荐室内运动，注意保暖。"},
        {"key": "clothes", "name": "穿衣", "info": "较冷", "detail": "建议着厚外套加毛衣等服装。"},
    ]
    data["source"] = "QQ 天气"
    data["sunrise"], data["sunset"] = "06:47", "17:28"

    image = web.render_weather_card(data)
    with Image.open(io.BytesIO(image)) as card:
        assert card.width == 900
        assert card.height > 600


def test_weather_card_renderer_tolerates_missing_fields() -> None:
    image = web.render_weather_card({"city": "武汉"})
    assert image.startswith(b"\x89PNG")


async def test_weather_command_sends_card_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "web", plugin_dir / "web")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("web")
        assert installed is not None
        module = sys.modules[installed.module.__name__ + ".plugin"]
        _patch_client(monkeypatch, module, qq_handler)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("/天气 武汉")), connection)

        segments = connection.sent[-1][2]
        images = [segment for segment in segments if segment.type == "image"]
        assert len(images) == 1
        assert images[0].data["file"].startswith("base64://")
        caption = "".join(segment.data.get("text", "") for segment in segments if segment.type == "text")
        assert "武汉" in caption and "雨" in caption
    finally:
        await bot.stop()


async def test_weather_command_falls_back_to_text_when_image_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "web", plugin_dir / "web")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("web")
        assert installed is not None
        await installed.settings.update({"weather_image": False})
        module = sys.modules[installed.module.__name__ + ".plugin"]
        _patch_client(monkeypatch, module, qq_handler)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("/天气 武汉")), connection)

        segments = connection.sent[-1][2]
        assert not [segment for segment in segments if segment.type == "image"]
        assert "湿度" in connection.last_text()
    finally:
        await bot.stop()


async def test_weather_command_uses_open_meteo_when_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "web", plugin_dir / "web")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("web")
        assert installed is not None
        await installed.settings.update({"weather_backend": "open-meteo", "weather_image": False})
        module = sys.modules[installed.module.__name__ + ".plugin"]
        _patch_client(monkeypatch, module, forecast_handler)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("/天气 武汉")), connection)

        text = connection.last_text()
        assert "多云" in text and "湿度" in text
    finally:
        await bot.stop()


async def test_weather_card_tool_sends_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "web", plugin_dir / "web")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("web")
        assert installed is not None
        module = sys.modules[installed.module.__name__ + ".plugin"]
        _patch_client(monkeypatch, module, qq_handler)
        spec = next(tool for tool in installed.ai_tools if tool.name == "weather_card")
        send_media = AsyncMock(return_value=True)
        monkeypatch.setattr(bot.deliveries, "send_media", send_media)
        ctx = Context(
            bot=bot,
            connection=FakeConnection(),
            event=parse_event(group_message("来张武汉天气图")),
            plugin=installed,
            handler=spec,
        )

        result = await spec.callback(ctx, city="武汉")

        assert result["sent"] is True
        assert result["city"] == "武汉"
        segments, content = send_media.call_args.args[1:3]
        assert [segment.type for segment in segments] == ["image"]
        assert "武汉" in content and "雨" in content
    finally:
        await bot.stop()


async def test_weather_card_tool_reports_disabled_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "web", plugin_dir / "web")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("web")
        assert installed is not None
        await installed.settings.update({"weather_image": False})
        spec = next(tool for tool in installed.ai_tools if tool.name == "weather_card")
        ctx = Context(
            bot=bot,
            connection=FakeConnection(),
            event=parse_event(group_message("来张武汉天气图")),
            plugin=installed,
            handler=spec,
        )
        result = await spec.callback(ctx, city="武汉")
        assert "关闭" in result["error"]
    finally:
        await bot.stop()


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
        llm={"enabled": True, "api_key": "fake-test-key", "context_window": 8192},
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
        assert {item["function"]["name"] for item in tools} == {"web__get_weather", "agent__stay_silent"}
    finally:
        await bot.stop()


async def test_tool_titles_are_exposed_to_console(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "web", plugin_dir / "web")

    bot = await make_bot(
        tmp_path,
        llm={"enabled": True, "api_key": "fake-test-key", "context_window": 8192},
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
            "web__weather_card": "天气图片",
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
        llm={"enabled": True, "api_key": "fake-test-key", "context_window": 8192},
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
            "agent__stay_silent",
            "web__web_search",
            "web__get_weather",
            "web__get_current_time",
        }
    finally:
        await bot.stop()
