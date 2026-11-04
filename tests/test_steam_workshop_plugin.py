"""Steam 创意工坊插件测试：链接解析、接口读取、网页回退、命令与工具注册（不访问网络）。"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot

ROOT = Path(__file__).resolve().parents[1]


def load_service() -> Any:
    spec = importlib.util.spec_from_file_location(
        "fllbot_test_steam_workshop_service", ROOT / "plugins" / "steam_workshop" / "service.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service = load_service()

ITEM_ID = "3775625682"

ITEM_DETAILS = {
    "publishedfileid": ITEM_ID,
    "result": 1,
    "creator": "76561199064454583",
    "consumer_app_id": 457140,
    "title": "LocalCanvas-本地画框",
    "description": "[h1]本地画布 - LocalCanvas[/h1][h2]简介[/h2]让画布显示本地图片。",
    "file_size": "421525",
    "preview_url": "https://images.steamusercontent.com/ugc/preview/",
    "time_created": 1785590008,
    "time_updated": 1785590008,
    "subscriptions": 158,
    "favorited": 22,
    "views": 774,
    "visibility": 0,
    "banned": 0,
    "tags": [{"tag": "ui"}, {"tag": "New Features"}],
}

PAGE_HTML = """
<html><body>
<div class="breadcrumbs"><a href="https://steamcommunity.com/app/457140">Oxygen Not Included</a>
  <span class="breadcrumb_separator">&gt;</span></div>
<div class="workshopItemTitle">LocalCanvas-本地画框</div>
<div class="creatorsBlock"><div class="friendBlockContent">妲己把茶倒你的嘴里<br>
  <span class="friendSmallText">在线</span></div></div>
<div class="workshopItemDescription"><div class="bb_h1">本地画布</div><br>
  让画布显示本地图片。</div>
<div class="detailsStatsContainerLeft"><div class="detailsStatLeft">文件大小</div></div>
<div class="detailsStatsContainerRight"><div class="detailsStatRight">421.525 KB</div></div>
<table class="stats_table">
  <tr><td>774</td><td>不重复访客数</td></tr>
  <tr><td>158</td><td>当前订阅者</td></tr>
  <tr><td>22</td><td>当前收藏人数</td></tr>
</table>
</body></html>
"""


def sample_item() -> dict[str, Any]:
    return {
        "id": ITEM_ID,
        "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={ITEM_ID}",
        "title": "LocalCanvas-本地画框",
        "kind": "item",
        "kind_label": "物品",
        "game": {"app_id": 457140, "name": "缺氧", "name_en": "Oxygen Not Included", "url": ""},
        "author": {"steam_id": "76561199064454583", "name": "测试作者", "url": ""},
        "tags": ["ui", "New Features"],
        "file_size": "411.6 KB",
        "file_size_bytes": 421525,
        "created_at": "2026-08-01 21:13",
        "updated_at": "2026-08-01 21:13",
        "stats": {
            "subscriptions": 158,
            "favorites": 22,
            "views": 774,
            "lifetime_subscriptions": 200,
            "lifetime_favorites": 24,
        },
        "description": "让画布显示本地图片。",
        "description_truncated": False,
        "preview_url": "",
        "children": [],
        "child_count": 0,
        "banned": False,
        "note": "",
    }


def use_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    original = httpx.AsyncClient

    def _new_client(timeout: float, proxy: str | None = None) -> httpx.AsyncClient:
        return original(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(service, "_new_client", _new_client)


def test_find_workshop_id_variants() -> None:
    url = "https://steamcommunity.com/sharedfiles/filedetails/?id=3775625682"
    assert service.find_workshop_id(url) == ITEM_ID
    assert service.find_workshop_id(f"帮我看看里面的内容 {url} 谢谢") == ITEM_ID
    assert service.find_workshop_id("https://www.steamcommunity.com/workshop/filedetails/?id=3775625682&x=1") == ITEM_ID
    assert service.find_workshop_id("3775625682") == ITEM_ID
    assert service.find_workshop_id("https://example.com/sharedfiles/filedetails/?id=3775625682") is None
    assert service.find_workshop_id("https://steamcommunity.com/sharedfiles/filedetails/?id=abc") is None
    with pytest.raises(service.WorkshopError):
        service.extract_id("没有链接")


def test_bbcode_to_text_cleans_lists_and_links() -> None:
    text = service.bbcode_to_text(
        "[h1]标题[/h1][b]粗体[/b][list][*]第一项[*][url=https://example.com]链接[/url][/list][hr][img]x[/img]"
    )
    assert "标题" in text
    assert "粗体" in text
    assert "- 第一项" in text
    assert "链接 (https://example.com)" in text
    assert "---" in text
    assert "[img]" not in text and "[url" not in text


def test_parse_page_extracts_metadata() -> None:
    page = service.parse_page(PAGE_HTML)
    assert page["title"] == "LocalCanvas-本地画框"
    assert page["game"] == "Oxygen Not Included"
    assert page["author"] == "妲己把茶倒你的嘴里"
    assert "让画布显示本地图片。" in page["description"]
    assert page["file_size"] == "421.525 KB"
    assert page["stats"] == {"views": 774, "subscriptions": 158, "favorites": 22}


async def test_load_item_uses_api_and_page(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "GetCollectionDetails" in str(request.url):
            return httpx.Response(
                200,
                json={"response": {"result": 1, "resultcount": 0,
                                   "collectiondetails": [{"publishedfileid": ITEM_ID, "result": 9}]}},
            )
        if request.method == "POST" and "GetPublishedFileDetails" in str(request.url):
            return httpx.Response(200, json={"response": {"result": 1, "publishedfiledetails": [ITEM_DETAILS]}})
        if request.url.host == "store.steampowered.com":
            return httpx.Response(200, json={"457140": {"success": True, "data": {"name": "缺氧"}}})
        return httpx.Response(200, text=PAGE_HTML, headers={"content-type": "text/html; charset=utf-8"})

    use_transport(monkeypatch, handler)
    data = await service.load_item(ITEM_ID, auto_proxy=False, timeout=5)

    assert data["title"] == "LocalCanvas-本地画框"
    assert data["game"]["name"] == "缺氧"
    assert data["game"]["name_en"] == "Oxygen Not Included"
    assert data["author"]["name"] == "妲己把茶倒你的嘴里"
    assert data["tags"] == ["ui", "New Features"]
    assert data["file_size"] == "411.6 KB"
    assert data["stats"]["views"] == 774
    assert data["description"].startswith("本地画布 - LocalCanvas")
    assert data["kind"] == "item"
    assert data["created_at"].startswith("2026-")


async def test_load_item_detects_collection_and_children(monkeypatch: pytest.MonkeyPatch) -> None:
    collection_id = "840498314"
    collection_details = {
        "response": {
            "result": 1,
            "resultcount": 1,
            "collectiondetails": [
                {
                    "publishedfileid": collection_id,
                    "result": 1,
                    "children": [
                        {"publishedfileid": "111", "sortorder": 1, "filetype": 2},
                        {"publishedfileid": "222", "sortorder": 2, "filetype": 2},
                        {"publishedfileid": "333", "sortorder": 3, "filetype": 2},
                    ],
                }
            ],
        }
    }
    children_details = {
        "response": {
            "result": 1,
            "publishedfiledetails": [
                {"publishedfileid": "111", "result": 1, "title": "第一项"},
                {"publishedfileid": "222", "result": 1, "title": "第二项"},
                {"publishedfileid": "333", "result": 1, "title": "第三项"},
            ],
        }
    }
    top = dict(ITEM_DETAILS, publishedfileid=collection_id, title="测试合集", description="")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "GetCollectionDetails" in str(request.url):
            return httpx.Response(200, json=collection_details)
        if request.method == "POST" and "GetPublishedFileDetails" in str(request.url):
            body = request.content.decode()
            return httpx.Response(200, json=children_details if "itemcount=3" in body else
                                  {"response": {"result": 1, "publishedfiledetails": [top]}})
        if request.url.host == "store.steampowered.com":
            return httpx.Response(200, json={"457140": {"success": True, "data": {"name": "缺氧"}}})
        return httpx.Response(200, text=PAGE_HTML, headers={"content-type": "text/html; charset=utf-8"})

    use_transport(monkeypatch, handler)
    data = await service.load_item(collection_id, auto_proxy=False, timeout=5)

    assert data["kind"] == "collection"
    assert data["kind_label"] == "合集"
    assert data["child_count"] == 3
    assert [child["title"] for child in data["children"]] == ["第一项", "第二项", "第三项"]
    text = service.format_item(data)
    assert "合集共 3 件物品" in text
    assert "2. 第二项" in text


async def test_load_item_reports_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"response": {"result": 1, "resultcount": 1,
                               "publishedfiledetails": [{"publishedfileid": ITEM_ID, "result": 9}]}},
        )

    use_transport(monkeypatch, handler)
    with pytest.raises(service.WorkshopNotFound, match="找不到"):
        await service.load_item(ITEM_ID, auto_proxy=False, timeout=5)


async def test_load_item_falls_back_to_page(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ConnectError("blocked", request=request)
        return httpx.Response(200, text=PAGE_HTML, headers={"content-type": "text/html; charset=utf-8"})

    use_transport(monkeypatch, handler)
    data = await service.load_item(ITEM_ID, auto_proxy=False, timeout=5)

    assert data["title"] == "LocalCanvas-本地画框"
    assert data["author"]["name"] == "妲己把茶倒你的嘴里"
    assert "官方接口" in data["note"]


async def test_load_item_network_error_mentions_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("blocked", request=request)

    use_transport(monkeypatch, handler)
    with pytest.raises(service.WorkshopError, match="代理"):
        await service.load_item(ITEM_ID, auto_proxy=False, timeout=5)


def test_format_item_summary() -> None:
    text = service.format_item(sample_item(), description_limit=200)
    assert "【Steam 创意工坊·物品】LocalCanvas-本地画框" in text
    assert "游戏：缺氧（Oxygen Not Included）" in text
    assert "订阅 158" in text and "浏览 774" in text
    assert "简介：" in text and "让画布显示本地图片。" in text
    assert f"链接：https://steamcommunity.com/sharedfiles/filedetails/?id={ITEM_ID}" in text
    assert "读取创意工坊失败：boom" == service.format_item({"error": "boom"})


async def test_package_registers_command_and_tool(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(
        ROOT / "plugins" / "steam_workshop",
        plugin_dir / "steam_workshop",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[plugin_dir])
    try:
        loaded = bot.plugins.get("steam_workshop")
        assert loaded is not None
        assert [tool.name for tool in loaded.ai_tools] == ["read_item", "find_comments"]
        assert loaded.ai_tools[0].qualified_name == "steam_workshop__read_item"
        assert any(spec.usage and "工坊" in spec.usage for spec in bot.dispatcher.command_handlers())
        assert loaded.readme.startswith("# steam_workshop")
    finally:
        await bot.stop()


async def test_link_messages_are_answered_through_the_agent_tool(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(
        ROOT / "plugins" / "steam_workshop",
        plugin_dir / "steam_workshop",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[plugin_dir])
    try:
        loaded = bot.plugins.get("steam_workshop")
        assert loaded is not None
        # 链接由智能体判断后调用工具读取；插件只注册 /工坊 命令，不再响应普通消息。
        assert loaded.handlers and all(spec.usage for spec in loaded.handlers)
        connection = FakeConnection()
        event = group_message(
            "帮我看看里面的内容 https://steamcommunity.com/sharedfiles/filedetails/?id=3775625682"
        )
        await bot.dispatcher.dispatch(parse_event(event), connection)
        assert connection.texts() == []
    finally:
        await bot.stop()


async def test_command_shows_usage_and_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(
        ROOT / "plugins" / "steam_workshop",
        plugin_dir / "steam_workshop",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[plugin_dir])
    try:
        loaded = bot.plugins.get("steam_workshop")
        assert loaded is not None

        async def fake_load(value: str, **kwargs: Any) -> dict[str, Any]:
            return sample_item()

        monkeypatch.setitem(loaded.ai_tools[0].callback.__globals__, "load_item", fake_load)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("/工坊")), connection)
        assert "用法：/工坊" in connection.last_text()

        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("/工坊 3775625682", user_id=30004)), connection)
        assert "LocalCanvas-本地画框" in connection.last_text()
    finally:
        await bot.stop()


async def test_agent_sees_authorized_workshop_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(
        ROOT / "plugins" / "steam_workshop",
        plugin_dir / "steam_workshop",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    bot = await make_bot(
        tmp_path,
        llm={"enabled": True, "api_key": "fake-test-key", "context_window": 8192},
        plugin_dirs=[plugin_dir],
    )
    try:
        agent = bot.plugins.get("agent")
        assert agent is not None
        await agent.settings.update(
            {
                "auto_group": True,
                "trigger_mode": "all",
                "cooldown_seconds": 0,
                "allowed_tools": ["steam_workshop__read_item"],
            }
        )
        complete = AsyncMock(return_value=SimpleNamespace(text="收到", tool_calls=[]))
        monkeypatch.setattr(bot.chat, "complete", complete)

        await bot.dispatcher.dispatch(parse_event(group_message("在吗")), FakeConnection())

        tools = complete.call_args.kwargs["tools"]
        assert {item["function"]["name"] for item in tools} == {
            "steam_workshop__read_item",
            "agent__stay_silent",
        }
    finally:
        await bot.stop()
