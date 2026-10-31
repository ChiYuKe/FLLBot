import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.llm.client import LLMClient, LLMError
from fllbot.onebot.models import parse_event
from helpers import FakeConnection, build_bot, group_message, make_bot

AUTH = {"Authorization": "Bearer admin-token"}
MODELS = {
    "enabled": True, "api_key": "fake-key", "base_url": "http://models.invalid",
    "context_mode": "custom", "default": "normal",
    "providers": {"normal": {"model": "normal-model"}, "technical": {"model": "technical-model"}},
}


def test_policy_roundtrip_inheritance_validation_and_restart(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, llm=MODELS)
    with TestClient(create_app(bot)) as client:
        client.patch("/api/plugins/agent/settings", headers=AUTH, json={"values": {"auto_group": True, "trigger_mode": "all"}})
        payload = {"enabled": True, "persona": "concise", "policy": {
            "name": "技术群策略", "trigger_mode": "at", "provider": "technical",
            "cooldown_seconds": 15, "allowed_tools": [], "system_extra": "本群讨论技术。",
        }}
        response = client.patch("/api/console/groups/123", headers=AUTH, json=payload)
        assert response.status_code == 200
        effective = response.json()["effective_policy"]
        assert effective["auto_group"] is True
        assert effective["trigger_mode"] == "at"
        assert effective["model"] == "technical-model"
        assert effective["persona"] == "concise"
        assert effective["configured"] is True
        assert client.get("/api/console/groups/456", headers=AUTH).json()["effective_policy"]["trigger_mode"] == "all"
        invalid = client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": False, "policy": {"provider": "missing"}})
        assert invalid.status_code == 400
        assert client.get("/api/console/groups/123", headers=AUTH).json()["enabled"] is True
        assert client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": True, "policy": {"cooldown_seconds": -1}}).status_code == 422
        assert client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": True, "policy": {"trigger_mode": "invalid"}}).status_code == 422
    with TestClient(create_app(build_bot(tmp_path, llm=MODELS))) as client:
        assert client.get("/api/console/groups/123", headers=AUTH).json()["policy"]["name"] == "技术群策略"
        client.patch("/api/config", headers=AUTH, json={"permission": {"deny_groups": [123]}})
        data = client.get("/api/console/groups/123", headers=AUTH).json()
        assert data["effective_policy"]["configured"] is False
        assert data["effective_policy"]["blocked_by"]
        response = client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": True, "policy": None})
        assert response.json()["policy"] is None
        assert response.json()["effective_policy"]["trigger_mode"] == "all"


async def test_group_policy_controls_actual_model_prompt_tools_and_cooldown(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, llm=MODELS)
    captured = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "收到"}}]})

    bot.chat.client = LLMClient(lambda: bot.config.llm, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    try:
        await bot.permissions.save_group(123, enabled=True, config={"policy": {
            "auto_group": True, "trigger_mode": "at", "provider": "technical", "cooldown_seconds": 60,
            "allowed_tools": [], "system_extra": "技术群专用规则",
        }})
        await bot.permissions.save_group(456, enabled=True, config={"policy": {
            "auto_group": True, "trigger_mode": "prefix", "trigger_prefix": "助手", "cooldown_seconds": 60,
            "allowed_tools": ["basic__check_online"], "system_extra": "闲聊群专用规则",
        }})
        connection = FakeConnection()
        connection.nickname = "助手"
        for payload in [
            group_message("路过", group_id=123),
            group_message("路过", group_id=456),
            group_message("帮我排查", group_id=123, at=10001),
            group_message("助手 在吗", group_id=456),
            group_message("再次询问", group_id=123, at=10001),
        ]:
            await bot.dispatcher.dispatch(parse_event(payload), connection)
        assert [body["model"] for body in captured] == ["technical-model", "normal-model"]
        assert "技术群专用规则" in captured[0]["messages"][0]["content"]
        assert "技术群专用规则" not in captured[1]["messages"][0]["content"]
        assert "闲聊群专用规则" in captured[1]["messages"][0]["content"]
        assert "tools" not in captured[0]
        assert captured[1]["tools"][0]["function"]["name"].startswith("basic__")
        assert captured[1]["messages"][-1]["content"] == "测试用户(30003，成员)：在吗"
        assert connection.texts() == ["收到", "收到"]
    finally:
        await bot.stop()


async def test_removed_policy_provider_does_not_fall_back_to_a_different_model(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, llm=MODELS)
    try:
        await bot.permissions.save_group(123, enabled=True, config={"policy": {"provider": "removed"}})
        with pytest.raises(LLMError, match="群策略"):
            await bot.chat.complete([{"role": "user", "content": "hello"}], scene="group", scene_id=123)
    finally:
        await bot.stop()


async def test_legacy_plugin_grants_migrate_to_tool_ids(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path)
    try:
        await bot.storage.kv_set("plugin-settings:agent", "0:allowed_plugins", ["basic"])
        await bot.permissions.save_group(
            456, enabled=True, config={"policy": {"allowed_plugins": ["basic"], "cooldown_seconds": 5}}
        )
    finally:
        await bot.stop()

    bot = await make_bot(tmp_path)
    try:
        agent = bot.plugins.get("agent")
        assert agent is not None
        raw = await bot.storage.kv_all("plugin-settings:agent")
        assert raw.get("0:allowed_tools") == ["basic__check_online"]
        assert "0:allowed_plugins" not in raw
        assert agent.settings.get("allowed_tools") == ["basic__check_online"]
        policy = bot.permissions.group_config(456)["policy"]
        assert policy["allowed_tools"] == ["basic__check_online"]
        assert "allowed_plugins" not in policy
    finally:
        await bot.stop()
