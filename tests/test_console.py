import time
from functools import partial
from pathlib import Path

from fastapi.testclient import TestClient

from fllbot.app import create_app
from helpers import build_bot

AUTH = {"Authorization": "Bearer admin-token"}


def test_multiple_reply_triggers_validate_persist_and_keep_on_detail_edit(tmp_path: Path) -> None:
    with TestClient(create_app(build_bot(tmp_path))) as client:
        payload = {"enabled": True, "policy": {"auto_group": True, "trigger_modes": ["at", "prefix"], "cooldown_seconds": 0}}
        result = client.patch("/api/console/groups/123", headers=AUTH, json=payload)
        assert result.status_code == 200
        assert result.json()["effective_policy"]["trigger_modes"] == ["at", "prefix"]
        policy = result.json()["policy"]
        policy["system_extra"] = "只修改详细说明"
        result = client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": True, "policy": policy})
        assert result.json()["policy"]["trigger_modes"] == ["at", "prefix"]
        for modes in ([], ["inherit"], ["off"], ["at", "prefix", "all", "at"]):
            assert client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": True, "policy": {"trigger_modes": modes}}).status_code == 422
    with TestClient(create_app(build_bot(tmp_path))) as client:
        result = client.get("/api/console/groups/123", headers=AUTH).json()
        assert result["policy"]["trigger_modes"] == ["at", "prefix"]
        assert result["policy"]["system_extra"] == "只修改详细说明"


def test_table_bulk_access_only_updates_selected_groups(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, overrides={"permission": {"allow_groups": [123, 456], "deny_groups": [789]}})
    with TestClient(create_app(bot)) as client:
        url = "/api/console/groups/batch-access"
        payload = {"group_ids": [123, 999, 123], "access": "deny"}
        assert client.patch(url, json=payload).status_code == 401
        result = client.patch(url, headers=AUTH, json=payload)
        assert result.status_code == 200
        assert [item["group_id"] for item in result.json()["items"]] == [123, 999]
        assert bot.config.permission.allow_groups == [456]
        assert bot.config.permission.deny_groups == [123, 789, 999]
        client.patch(url, headers=AUTH, json={"group_ids": [123, 999], "access": "inherit"})
        assert bot.config.permission.allow_groups == [456] and bot.config.permission.deny_groups == [789]
        client.patch(url, headers=AUTH, json={"group_ids": [456], "access": "inherit"})
        assert bot.config.permission.allow_groups == [] and bot.config.permission.group_access == "listed"
        assert bot.permissions.group_enabled(123) is False
        assert client.patch(url, headers=AUTH, json={"group_ids": [], "access": "allow"}).status_code == 422
        assert client.patch(url, headers=AUTH, json={"group_ids": [-1], "access": "allow"}).status_code == 400


def test_access_in_configuration_table_preserves_existing_lists_and_rules(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, overrides={"permission": {"allow_groups": [123, 456], "deny_groups": [789]}})
    with TestClient(create_app(bot)) as client:
        assert client.get("/api/console/access").status_code == 401
        assert client.get("/api/console/access", headers=AUTH).json() == {"mode": "listed"}
        items = client.get("/api/console/groups?group_ids=123&group_ids=789&group_ids=999", headers=AUTH).json()["items"]
        assert [(item["access"], item["has_override"]) for item in items] == [("allow", True), ("deny", True), ("inherit", False)]
        assert bot.permissions.group_enabled(999) is False
        client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": True, "persona": "concise", "policy": {"cooldown_seconds": 0}})
        changed = client.patch("/api/console/groups/123/access", headers=AUTH, json={"access": "deny"}).json()
        assert changed["denied"] and changed["policy"]["cooldown_seconds"] == 0 and changed["persona"] == "concise"
        config = client.get("/api/config", headers=AUTH).json()["config"]["permission"]
        assert config["allow_groups"] == [456] and config["deny_groups"] == [123, 789]
        client.patch("/api/console/groups/123/access", headers=AUTH, json={"access": "allow"})
        assert bot.permissions.group_enabled(123) is True
        assert client.patch("/api/console/access", headers=AUTH, json={"mode": "all"}).status_code == 200
        assert bot.permissions.group_enabled(999) is True
        assert bot.permissions.group_enabled(789) is False
        assert bot.config.permission.allow_groups == [123, 456]
        client.patch("/api/console/access", headers=AUTH, json={"mode": "listed"})
        client.patch("/api/console/groups/456/access", headers=AUTH, json={"access": "inherit"})
        client.delete("/api/console/groups/123/rule", headers=AUTH)
        assert bot.config.permission.allow_groups == []
        assert bot.config.permission.group_access == "listed"
        assert bot.permissions.group_enabled(123) is False and bot.permissions.group_enabled(999) is False
        assert bot.config.permission.deny_groups == [789]
        assert client.patch("/api/console/groups/123/access", headers=AUTH, json={"access": "invalid"}).status_code == 422
        assert client.patch("/api/console/access", headers=AUTH, json={"mode": "invalid"}).status_code == 422
    restored_bot = build_bot(tmp_path)
    with TestClient(create_app(restored_bot)) as client:
        assert client.get("/api/console/access", headers=AUTH).json()["mode"] == "listed"
        assert restored_bot.permissions.group_enabled(999) is False
        assert restored_bot.config.permission.deny_groups == [789]


def test_explicit_group_rules_create_remove_and_restart(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, overrides={"permission": {"deny_groups": [456]}})
    with TestClient(create_app(bot)) as client:
        assert client.portal is not None
        client.portal.call(bot.permissions.ensure_group, 123)
        for _ in range(2):
            items = client.get("/api/console/groups?group_ids=123&group_ids=456", headers=AUTH).json()["items"]
            assert items[0]["has_override"] is False
            assert items[1]["has_override"] is True and items[1]["access"] == "deny"
        assert "_console_rule" not in bot.permissions.group_config(123)
        assert client.post("/api/console/groups/123/rule").status_code == 401
        created = client.post("/api/console/groups/123/rule", headers=AUTH).json()
        assert created["has_override"] is True
        assert created["policy"] is None
        assert created["persona"] == ""
        assert client.post("/api/console/groups/123/rule", headers=AUTH).status_code == 200
        client.patch("/api/groups/123/config", headers=AUTH, json={"unrelated": {"keep": 7}, "llm": {"other": 8}})
        client.patch("/api/console/groups/123", headers=AUTH, json={
            "enabled": False, "persona": "concise", "llm_enabled": False,
            "policy": {"auto_group": False, "allowed_tools": []},
        })
        client.patch("/api/plugins/agent/settings", headers=AUTH, json={
            "group_id": 123, "values": {"auto_group": False, "trigger_mode": "at"},
        })
        removed = client.delete("/api/console/groups/123/rule", headers=AUTH).json()
        assert removed["has_override"] is False
        assert removed["policy"] is None and removed["persona"] == "" and removed["llm_enabled"] is None
        assert bot.plugins.get("agent").settings.group_overrides(123) == {}
        assert bot.permissions.group_config(123)["unrelated"] == {"keep": 7}
        assert bot.permissions.group_config(123)["llm"] == {"other": 8}
        assert client.get("/api/config", headers=AUTH).json()["config"]["permission"]["deny_groups"] == [456]
        client.patch("/api/config", headers=AUTH, json={"permission": {"default_group_enabled": False}})
        assert bot.permissions.group_enabled(123) is False
        assert client.get("/api/console/groups/123", headers=AUTH).json()["has_override"] is False
    with TestClient(create_app(build_bot(tmp_path))) as client:
        restored = client.get("/api/console/groups/123", headers=AUTH).json()
        assert restored["has_override"] is False and restored["enabled"] is False
        assert restored["policy"] is None and restored["inherited_policy"]["trigger_mode"] == "all"


def test_existing_custom_settings_are_kept_but_defaults_not_listed(tmp_path: Path) -> None:
    with TestClient(create_app(build_bot(tmp_path))) as client:
        client.patch("/api/groups/123/config", headers=AUTH, json={"policy": {"cooldown_seconds": 0}})
        client.patch("/api/plugins/agent/settings", headers=AUTH, json={"group_id": 456, "values": {"auto_group": True}})
        items = client.get("/api/console/groups?group_ids=123&group_ids=456&group_ids=789", headers=AUTH).json()["items"]
        assert [item["has_override"] for item in items] == [True, True, False]
        assert client.delete("/api/console/groups/456/rule", headers=AUTH).json()["has_override"] is False


def test_group_table_bulk_read_and_inline_update_preserve_details(tmp_path: Path) -> None:
    with TestClient(create_app(build_bot(tmp_path))) as client:
        url = "/api/console/groups?group_ids=123&group_ids=456&group_ids=123"
        assert client.get(url).status_code == 401
        items = client.get(url, headers=AUTH).json()["items"]
        assert [item["group_id"] for item in items] == [123, 456]
        assert client.get("/api/console/groups?group_ids=-1", headers=AUTH).status_code == 400
        assert client.get("/api/console/groups?group_ids=abc", headers=AUTH).status_code == 422
        assert client.get("/api/console/groups?" + "&".join("group_ids=123" for _ in range(201)), headers=AUTH).status_code == 422
        client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": True, "persona": "concise", "policy": {
            "system_extra": "保留本群说明", "allowed_tools": [], "cooldown_seconds": 0,
        }})
        client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": False, "persona": "concise"})
        saved = client.get(url, headers=AUTH).json()["items"][0]
        assert saved["enabled"] is False
        assert saved["policy"]["system_extra"] == "保留本群说明"
        assert saved["policy"]["allowed_tools"] == []
        assert saved["policy"]["cooldown_seconds"] == 0


def test_console_readiness_and_auth(tmp_path: Path) -> None:
    bot = build_bot(tmp_path)
    with TestClient(create_app(bot)) as client:
        assert client.get("/api/console/readiness").status_code == 401
        data = client.get("/api/console/readiness", headers=AUTH).json()
        assert data["account_connected"] is False
        assert data["model_configured"] is False
        assert data["agent_enabled"] is True
        assert data["global_response"] is False
        assert data["responding_groups"] == []
        assert client.get("/api/console/sessions", headers=AUTH).json() == {"items": []}


def test_group_settings_preserve_existing_config_and_reset_overrides(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, overrides={"permission": {"deny_groups": [123]}})
    with TestClient(create_app(bot)) as client:
        client.patch("/api/groups/123/config", headers=AUTH, json={"custom": {"keep": True}, "llm": {"other": 7}})
        response = client.patch("/api/console/groups/123", headers=AUTH, json={
            "enabled": True, "llm_enabled": False, "persona": "fllbot",
        })
        assert response.status_code == 200
        assert response.json()["effective_enabled"] is False
        assert response.json()["enabled"] is True
        assert response.json()["denied"] is True
        saved = client.get("/api/groups/123/config", headers=AUTH).json()["config"]
        assert saved == {"custom": {"keep": True}, "llm": {"other": 7, "enabled": False, "persona": "fllbot"}, "_console_rule": True}
        response = client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": False})
        assert response.json()["llm_enabled"] is None
        assert response.json()["persona"] == ""
        saved = client.get("/api/groups/123/config", headers=AUTH).json()["config"]
        assert saved == {"custom": {"keep": True}, "llm": {"other": 7}, "_console_rule": True}
        invalid = client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": True, "persona": "missing"})
        assert invalid.status_code == 400
        assert client.get("/api/console/groups/123", headers=AUTH).json()["enabled"] is False
        assert client.get("/api/console/groups/0", headers=AUTH).status_code == 422


def test_group_settings_persist_after_restart(tmp_path: Path) -> None:
    with TestClient(create_app(build_bot(tmp_path))) as client:
        response = client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": False, "persona": "fllbot"})
        assert response.status_code == 200
    with TestClient(create_app(build_bot(tmp_path))) as client:
        data = client.get("/api/console/groups/123", headers=AUTH).json()
        assert data["enabled"] is False
        assert data["persona"] == "fllbot"


def test_config_overrides_do_not_expose_keys(tmp_path: Path) -> None:
    bot = build_bot(tmp_path)
    with TestClient(create_app(bot)) as client:
        response = client.patch("/api/config", headers=AUTH, json={"llm": {
            "api_key": "super-secret-llm-key", "providers": {"demo": {"api_key": "provider-secret-key"}},
        }})
        assert response.status_code == 200
        response = client.get("/api/config", headers=AUTH)
        for secret in ("super-secret-llm-key", "provider-secret-key", "admin-token", "test-token"):
            assert secret not in response.text
        assert response.json()["overrides"]["llm"]["api_key"]


def test_readiness_counts_group_overrides(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, llm={"enabled": True, "api_key": "fake-key"})
    with TestClient(create_app(bot)) as client:
        client.patch("/api/console/groups/123", headers=AUTH, json={"enabled": True})
        client.patch("/api/plugins/agent/settings", headers=AUTH, json={
            "group_id": 123, "values": {"auto_group": True},
        })
        data = client.get("/api/console/readiness", headers=AUTH).json()
        assert data["global_response"] is False
        assert data["responding_groups"] == [123]
        client.patch("/api/config", headers=AUTH, json={"permission": {"deny_groups": [123]}})
        assert client.get("/api/console/readiness", headers=AUTH).json()["responding_groups"] == []


def test_session_directory_and_history(tmp_path: Path) -> None:
    bot = build_bot(tmp_path)
    with TestClient(create_app(bot)) as client:
        assert client.portal is not None
        client.portal.call(
            partial(
                bot.storage.add_chat_message,
                "group", 123, "user", "测试消息",
                user_id=456, nickname="小明", sender_role="owner",
            )
        )
        client.portal.call(bot.storage.add_chat_message, "group", 123, "assistant", "测试回复")
        directory = client.get("/api/console/sessions", headers=AUTH).json()["items"]
        assert directory[0]["scene"] == "group"
        assert directory[0]["scene_id"] == 123
        assert directory[0]["messages"] == 2
        assert "测试消息" not in str(directory)
        history = client.get("/api/chat/group/123", headers=AUTH).json()["messages"]
        assert [item["content"] for item in history] == ["测试消息", "测试回复"]
        assert (history[0]["nickname"], history[0]["sender_role"]) == ("小明", "owner")
        assert client.delete("/api/chat/group/123", headers=AUTH).status_code == 200
        assert client.get("/api/console/sessions", headers=AUTH).json() == {"items": []}


async def test_message_trend_ignores_old_active_days(tmp_path: Path) -> None:
    bot = build_bot(tmp_path)
    await bot.storage.connect()
    try:
        await bot.storage.record_message(self_id=1, scene="group", scene_id=123, user_id=2, content="今天的消息")
        await bot.storage.db.execute(
            "INSERT INTO message_log (self_id, scene, scene_id, user_id, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "group", 123, 2, "两周前的消息", time.time() - 14 * 86400),
        )
        await bot.storage.db.commit()
        assert sum(item["count"] for item in await bot.storage.daily_message_counts(7)) == 1
    finally:
        await bot.storage.close()
