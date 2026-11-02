from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.policy import effective_policy
from helpers import FakeConnection, build_bot, make_bot, seed_personas

AUTH = {"Authorization": "Bearer admin-token"}
LLM = {"enabled": True, "api_key": "fake-key", "model": "test-model"}


async def test_sources_follow_actual_inheritance_and_keep_false_and_empty_overrides(tmp_path: Path) -> None:
    seed_personas(tmp_path, "concise")
    bot = await make_bot(tmp_path, llm=LLM)
    try:
        await bot.bots.update(10001, defaults={"reply_mode": "prefix", "wake_word": "助手", "persona": "concise"})
        agent = bot.plugins.get("agent")
        await agent.settings.update({"auto_group": False, "cooldown_seconds": 12}, group_id=123)
        await bot.permissions.save_group(123, enabled=True, config={"policy": {
            "auto_group": True, "allowed_tools": [], "cooldown_seconds": 0, "trigger_modes": ["at", "prefix"],
        }})
        result = effective_policy(bot, 123, 10001)
        assert result["auto_group"] is True
        assert result["cooldown_seconds"] == 0
        assert result["sources"]["auto_group"] == "group"
        assert result["sources"]["allowed_tools"] == "group"
        assert result["sources"]["cooldown_seconds"] == "group"
        assert result["sources"]["trigger_modes"] == "group"
        assert result["sources"]["persona"] == "bot"
        assert result["sources"]["trigger_prefix"] == "bot"
        assert result["configured"] is True
        assert result["diagnostics"]["ready"] is False
        await bot.permissions.save_group(123, enabled=True, config={})
        inherited = effective_policy(bot, 123, 10001)
        assert inherited["auto_group"] is False
        assert inherited["sources"]["auto_group"] == "legacy_group"
        assert inherited["sources"]["cooldown_seconds"] == "system"
        assert inherited["cooldown_seconds"] == 12
        assert effective_policy(bot, 456, 10002)["sources"]["persona"] == "system"
    finally:
        await bot.stop()


async def test_diagnostics_report_all_hard_limits_without_network_calls(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, llm=LLM)
    try:
        await bot.bots.update(10001, defaults={"reply_mode": "at"})
        conn = FakeConnection(10001)
        bot.manager.add(conn)
        assert effective_policy(bot, 123, 10001)["diagnostics"]["ready"] is True
        await bot.permissions.ensure_group(123)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url="http://test") as client:
            response = await client.get("/api/console/readiness", headers=AUTH)
            assert 123 in response.json()["responding_groups"]
            assert 123 in response.json()["ready_groups"]
            await conn.close()
            offline = (await client.get("/api/console/readiness", headers=AUTH)).json()
            assert 123 in offline["responding_groups"]
            assert 123 not in offline["ready_groups"]
        await bot.permissions.save_group(123, enabled=False, config={"llm": {"enabled": False}})
        await bot.permissions.set_plugin_enabled("agent", False, 123)
        await bot.update_config({"permission": {"deny_groups": [123]}, "llm": {"enabled": False, "enable_group": False}})
        result = effective_policy(bot, 123, 10001)
        assert set(result["blocked_codes"]) >= {
            "group_denied", "group_disabled", "scene_ai_disabled", "agent_unavailable", "llm_disabled", "group_ai_disabled",
        }
        assert result["configured"] is False
        assert result["diagnostics"]["ready"] is False
        assert conn.sent == []
    finally:
        await bot.stop()


def test_api_diagnostics_auth_bot_context_and_validation(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, llm=LLM)
    with TestClient(create_app(bot)) as client:
        assert client.get("/api/console/groups/123").status_code == 401
        assert client.get("/api/console/groups/123?self_id=0", headers=AUTH).status_code == 422
        response = client.get("/api/console/groups/123?self_id=10002", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["self_id"] == 10002
        assert data["effective_policy"]["diagnostics"]["checks"][0]["code"] == "bot_offline"
        assert data["effective_policy"]["diagnostics"]["ready"] is False
        assert "fake-key" not in response.text
