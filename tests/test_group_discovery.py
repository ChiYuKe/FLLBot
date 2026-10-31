from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fllbot.app import create_app
from helpers import FakeConnection, build_bot

AUTH = {"Authorization": "Bearer admin-token"}


class GroupConnection(FakeConnection):
    def __init__(self, self_id, groups=None, error=False):
        super().__init__(self_id)
        self.groups = groups or []
        self.error = error

    async def call(self, action, params=None, **kwargs):
        assert action == "get_group_list"
        if self.error:
            raise ConnectionError("account disconnected")
        return self.groups


def test_group_discovery_merges_accounts_and_keeps_saved_ids(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=False, overrides={"permission": {"allow_groups": [12345, 88888], "deny_groups": [99999]}})
    with TestClient(create_app(bot)) as api:
        bot.manager.add(GroupConnection(10001, [
            {"group_id": 12345, "group_name": "测试群", "member_count": 20},
            {"group_id": "23456", "group_name": "新群"},
            {"group_id": 0}, {"group_id": "invalid"}, "invalid",
        ]))
        bot.manager.add(GroupConnection(10002, [{"group_id": 12345, "group_name": "测试群", "member_count": 21}]))
        bot.manager.add(GroupConnection(10003, error=True))
        assert api.get('/api/groups/discover').status_code == 401
        response = api.get('/api/groups/discover', headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        groups = {item['group_id']: item for item in data['groups']}
        assert set(groups) == {12345, 23456, 88888, 99999}
        assert groups[12345]['self_ids'] == [10001, 10002]
        assert groups[12345]['member_count'] == 21
        assert groups[88888]['live'] is False
        assert groups[99999]['denied'] is True
        assert data['failed_accounts'] == 1
        assert data['online_accounts'] == 3
        # Reading membership must not automatically allow new groups or write group state.
        assert bot.config.permission.allow_groups == [12345, 88888]
        assert bot.permissions.list_groups() == []
        assert bot.permissions.group_enabled(23456) is False


def test_group_discovery_offline_and_whitelist_save(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=False, overrides={"permission": {"allow_groups": [12345], "deny_groups": [23456]}})
    with TestClient(create_app(bot)) as api:
        data = api.get('/api/groups/discover', headers=AUTH).json()
        assert data['online_accounts'] == 0
        assert {item['group_id'] for item in data['groups']} == {12345, 23456}
        assert all(item['live'] is False for item in data['groups'])
        response = api.patch('/api/config', headers=AUTH, json={"permission": {"allow_groups": [12345, 23456, 34567]}})
        assert response.status_code == 200
        assert bot.permissions.group_enabled(34567) is True
        assert bot.permissions.group_enabled(45678) is False
        assert bot.permissions.group_enabled(23456) is False  # deny still wins
        assert bot.config.permission.deny_groups == [23456]
