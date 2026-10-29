"""管理 API 测试（TestClient 会走完整 lifespan）。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fllbot.app import create_app
from helpers import build_bot

AUTH = {"Authorization": "Bearer admin-token"}


def test_health_and_auth(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=True)
    with TestClient(create_app(bot)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert health.json()["version"]

        assert client.get("/api/status").status_code == 401
        assert client.get("/api/plugins", headers={"X-Admin-Token": "wrong"}).status_code == 401

        status = client.get("/api/status", headers=AUTH)
        assert status.status_code == 200
        data = status.json()
        names = {item["name"] for item in data["plugins"]}
        assert {"ping", "echo", "help", "llm_chat"} <= names
        assert data["storage"]["schema_version"] >= 1
        assert data["ws_path"] == "/onebot/v11/ws"


def test_token_via_query_param(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=False)
    with TestClient(create_app(bot)) as client:
        assert client.get("/api/status?token=admin-token").status_code == 200


def test_plugin_toggle_and_reload(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=True)
    with TestClient(create_app(bot)) as client:
        detail = client.get("/api/plugins/ping", headers=AUTH)
        assert detail.status_code == 200
        assert detail.json()["enabled"] is True

        toggled = client.post("/api/plugins/ping/enabled", headers=AUTH, json={"enabled": False})
        assert toggled.status_code == 200
        assert client.get("/api/plugins/ping", headers=AUTH).json()["enabled"] is False

        reloaded = client.post("/api/plugins/reload", headers=AUTH, json={"name": "ping"})
        assert reloaded.status_code == 200
        assert reloaded.json()["reloaded"] == "ping"

        missing = client.post("/api/plugins/nope/reload", headers=AUTH)
        assert missing.status_code == 404


def test_config_patch(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=False)
    with TestClient(create_app(bot)) as client:
        current = client.get("/api/config", headers=AUTH).json()
        assert current["config"]["llm"]["api_key"] == ""

        patched = client.patch("/api/config", headers=AUTH, json={"llm": {"model": "demo-model", "api_key": "sk-123"}})
        assert patched.status_code == 200
        assert patched.json()["config"]["llm"]["model"] == "demo-model"
        assert "sk-123" not in str(patched.json()["config"]["llm"]["api_key"])

        again = client.get("/api/config", headers=AUTH).json()
        assert again["config"]["llm"]["model"] == "demo-model"
        assert again["overrides"]["llm"]["model"] == "demo-model"


def test_groups_and_kv(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=False)
    with TestClient(create_app(bot)) as client:
        toggled = client.post("/api/groups/20002/enabled", headers=AUTH, json={"enabled": False})
        assert toggled.status_code == 200

        groups = client.get("/api/groups", headers=AUTH).json()
        assert any(item["group_id"] == 20002 and item["enabled"] is False for item in groups)

        patched = client.patch("/api/groups/20002/config", headers=AUTH, json={"llm": {"persona": "concise"}})
        assert patched.status_code == 200
        assert patched.json()["config"]["llm"]["persona"] == "concise"

        assert client.put("/api/kv/demo/key", headers=AUTH, json={"value": 123}).status_code == 200
        assert client.get("/api/kv/demo", headers=AUTH).json() == {"key": 123}


def test_personas_and_stats(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=True)
    with TestClient(create_app(bot)) as client:
        personas = client.get("/api/personas", headers=AUTH).json()
        assert any(item["name"] == "fllbot" for item in personas)

        stats = client.get("/api/stats", headers=AUTH).json()
        assert "daily" in stats and "dispatcher" in stats

        logs = client.get("/api/logs?limit=5", headers=AUTH).json()
        assert isinstance(logs["items"], list)


def test_action_requires_connection(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=False)
    with TestClient(create_app(bot)) as client:
        response = client.post("/api/actions", headers=AUTH, json={"action": "get_login_info"})
        assert response.status_code == 503
