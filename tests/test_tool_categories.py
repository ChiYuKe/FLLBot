import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.plugin import Plugin
from helpers import build_bot

AUTH = {"Authorization": "Bearer admin-token"}


def tool_plugins(tmp_path: Path) -> Path:
    target = tmp_path / "extensions"
    target.mkdir()
    source = Path(__file__).resolve().parents[1] / "plugins"
    for name in ("voice", "web"):
        shutil.copytree(source / name, target / name)
    return target


def test_tool_categories_and_individual_grants_persist(tmp_path: Path) -> None:
    directory = tool_plugins(tmp_path)
    bot = build_bot(tmp_path, plugin_dirs=[directory])
    with TestClient(create_app(bot)) as client:
        plugins = {item["name"]: item for item in bot.plugin_summaries()}
        assert plugins["voice"]["category"] == plugins["web"]["category"] == plugins["basic"]["category"] == "plugin"
        catalog = client.get("/api/tools", headers=AUTH).json()
        assert {item["name"] for item in catalog["providers"]} == {"basic", "voice", "web"}
        assert {item["id"] for item in catalog["items"]} >= {"voice__speak", "web__web_search", "web__read_webpage", "web__get_weather", "web__get_current_time"}
        url = "/api/tools/web__web_search/enabled"
        assert client.patch(url, json={"enabled": True}).status_code == 401
        assert client.patch("/api/tools/missing__tool/enabled", headers=AUTH, json={"enabled": True}).status_code == 404
        result = client.patch(url, headers=AUTH, json={"enabled": True})
        assert result.status_code == 200
        assert result.json()["allowed_tools"] == ["web__web_search"]
        catalog = client.get("/api/tools", headers=AUTH).json()
        assert [item["id"] for item in catalog["items"] if item["globally_allowed"]] == ["web__web_search"]
        client.post("/api/plugins/web/enabled", headers=AUTH, json={"enabled": False})
        assert not any(item["plugin_enabled"] for item in client.get("/api/tools", headers=AUTH).json()["items"] if item["plugin"] == "web")
        client.post("/api/plugins/web/enabled", headers=AUTH, json={"enabled": True})
    with TestClient(create_app(build_bot(tmp_path, plugin_dirs=[directory]))) as client:
        catalog = client.get("/api/tools", headers=AUTH).json()
        assert [item["id"] for item in catalog["items"] if item["globally_allowed"]] == ["web__web_search"]
        assert client.patch("/api/tools/web__web_search/enabled", headers=AUTH, json={"enabled": False}).json()["allowed_tools"] == []


def test_category_defaults_and_validation() -> None:
    assert Plugin(name="normal").metadata()["category"] == "plugin"
    assert Plugin(name="utility", category="tool").metadata()["category"] == "tool"
    with pytest.raises(ValueError):
        Plugin(name="bad", category="invalid")
