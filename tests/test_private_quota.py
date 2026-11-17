"""私聊智能体按「Bot 账号 + 用户」每日 Token / 费用额度。"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.llm.client import LLMError
from fllbot.onebot.models import parse_event
from fllbot.storage import MIGRATIONS, SCHEMA_VERSION, Storage
from helpers import FakeConnection, build_bot, make_bot, private_message

AUTH = {"Authorization": "Bearer admin-token"}
MESSAGES = [{"role": "system", "content": "rules"}, {"role": "user", "content": "current"}]
LLM = {"enabled": True, "api_key": "k", "context_window": 8192, "max_tokens": 20}


def model_response(tokens: int = 15) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "ok", "tool_calls": []}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tokens - 5, "completion_tokens": 5},
        },
    )


def mock_transport(captured: list[httpx.Request], *, tokens: int = 850):
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: captured.append(request) or model_response(tokens)))


def today_midnight() -> float:
    return datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def test_private_quota_config_api_defaults_validation_and_persistence(tmp_path):
    with TestClient(create_app(build_bot(tmp_path))) as client:
        data = client.get("/api/config", headers=AUTH).json()
        assert data["config"]["llm"]["private_daily_token_limit"] == 0
        assert data["config"]["llm"]["private_daily_cost_limit"] == 0.0
        revision = data["revision"]
        invalid = client.patch("/api/config", headers={**AUTH, "If-Match": revision},
                               json={"llm": {"private_daily_token_limit": -1}})
        assert invalid.status_code == 400 and "配置无效" in invalid.json()["detail"]
        saved = client.patch("/api/config", headers={**AUTH, "If-Match": revision},
                             json={"llm": {"private_daily_token_limit": 1000, "private_daily_cost_limit": 12.5}})
        assert saved.status_code == 200
        llm = saved.json()["config"]["llm"]
        assert llm["private_daily_token_limit"] == 1000
        assert llm["private_daily_cost_limit"] == 12.5
        stale = client.patch("/api/config", headers={**AUTH, "If-Match": revision},
                             json={"llm": {"private_daily_token_limit": 5}})
        assert stale.status_code == 409
    with TestClient(create_app(build_bot(tmp_path))) as client:
        llm = client.get("/api/config", headers=AUTH).json()["config"]["llm"]
        assert llm["private_daily_token_limit"] == 1000
        assert llm["private_daily_cost_limit"] == 12.5


async def test_private_usage_storage_is_scoped_and_windowed(tmp_path):
    storage = Storage(tmp_path / "usage.db")
    await storage.connect()
    try:
        async def add(scene: str, scene_id: int, self_id: int, tokens: int) -> None:
            await storage.record_llm_usage(
                scene=scene, scene_id=scene_id, self_id=self_id, provider="p", model="m",
                prompt_tokens=tokens - 5, completion_tokens=5, latency_ms=1.0,
            )

        await add("private", 30003, 10001, 100)
        await add("private", 30003, 10002, 200)  # 同一个用户的另一个 Bot
        await add("private", 40004, 10001, 300)  # 同一个 Bot 的另一个用户
        await add("group", 30003, 10001, 400)
        await add("group", 30003, 10002, 500)
        since = time.time() - 3600
        assert await storage.private_llm_usage(30003, since, self_id=10001) == {"tokens": 100, "cost": 0.0}
        assert (await storage.private_llm_usage(30003, since, self_id=10002))["tokens"] == 200
        assert (await storage.scene_llm_usage("private", 30003, since))["tokens"] == 300
        # 旧群额度口径保留：同群多 Bot 共享，不按 self_id 拆分。
        assert (await storage.group_llm_usage(30003, since))["tokens"] == 900
        assert await storage.schema_version() == SCHEMA_VERSION
        columns = [row[1] for row in await (await storage.db.execute("PRAGMA table_info(llm_usage)")).fetchall()]
        assert "self_id" in columns
        # 跨日窗口：昨天的记账不计入今天。
        await storage.db.execute("UPDATE llm_usage SET created_at = ?", (time.time() - 86400,))
        await storage.db.commit()
        assert (await storage.private_llm_usage(30003, today_midnight(), self_id=10001))["tokens"] == 0
    finally:
        await storage.close()


async def test_schema_27_usage_rows_upgrade_without_breaking_group_quota(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        for version in sorted(MIGRATIONS):
            if version <= 27:
                for statement in MIGRATIONS[version]:
                    connection.execute(statement)
        connection.execute("PRAGMA user_version = 27")
        connection.execute(
            "INSERT INTO llm_usage (scene,scene_id,provider,model,prompt_tokens,completion_tokens,"
            "latency_ms,cost,ok,error,created_at,usage_estimated,turn_id,cached_prompt_tokens) "
            "VALUES ('group',1,'p','m',100,0,1,0,1,'',?,0,'',NULL)",
            (time.time(),),
        )
    storage = Storage(path)
    await storage.connect()
    try:
        assert await storage.schema_version() == SCHEMA_VERSION
        since = time.time() - 3600
        assert (await storage.group_llm_usage(1, since))["tokens"] == 100
        # 升级前的记录没有 Bot 账号，不并入任何私聊用户的额度。
        assert (await storage.private_llm_usage(1, since, self_id=0))["tokens"] == 0
    finally:
        await storage.close()


async def test_private_quota_blocks_before_request_and_isolates_scope(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, llm={
        **LLM, "private_daily_token_limit": 1000, "group_daily_token_limit": 1000})
    captured: list[httpx.Request] = []
    bot.chat.client._http = mock_transport(captured)
    try:
        results = await asyncio.gather(
            *[bot.chat.complete(MESSAGES, scene="private", scene_id=30003, self_id=10001) for _ in range(2)],
            return_exceptions=True,
        )
        errors = [item for item in results if isinstance(item, LLMError)]
        assert len(errors) == 1 and len(captured) == 1
        assert "私聊" in str(errors[0]) and "Token 额度不足" in str(errors[0])
        # 另一个用户、另一个 Bot 各自独立计数。
        await bot.chat.complete(MESSAGES, scene="private", scene_id=40004, self_id=10001)
        await bot.chat.complete(MESSAGES, scene="private", scene_id=30003, self_id=10002)
        assert len(captured) == 3
        # 群额度与私聊额度互不影响：同号群首次可用，用满后才拒绝。
        await bot.chat.complete(MESSAGES, scene="group", scene_id=30003, self_id=10001)
        assert len(captured) == 4
        with pytest.raises(LLMError, match="本群今日 Token 额度不足"):
            await bot.chat.complete(MESSAGES, scene="group", scene_id=30003, self_id=10001)
        assert len(captured) == 4
        # 群用量也不会反过来占掉该 Bot 的私聊额度。
        with pytest.raises(LLMError, match="该用户今日私聊 Token 额度不足"):
            await bot.chat.complete(MESSAGES, scene="private", scene_id=30003, self_id=10002)
        assert len(captured) == 4
        rows = await (await bot.storage.db.execute(
            "SELECT DISTINCT self_id, scene FROM llm_usage ORDER BY scene")).fetchall()
        assert {(row["self_id"], row["scene"]) for row in rows} == {
            (10001, "group"), (10001, "private"), (10002, "private")}
    finally:
        await bot.stop()


async def test_private_quota_zero_means_unlimited_and_records_bot(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, llm=LLM)
    captured: list[httpx.Request] = []
    bot.chat.client._http = mock_transport(captured, tokens=15)
    try:
        for _ in range(3):
            await bot.chat.complete(MESSAGES, scene="private", scene_id=30003, self_id=10001)
        assert len(captured) == 3
        rows = await (await bot.storage.db.execute(
            "SELECT self_id, scene, scene_id FROM llm_usage ORDER BY id")).fetchall()
        assert [(row["self_id"], row["scene"], row["scene_id"]) for row in rows] == [
            (10001, "private", 30003)] * 3
    finally:
        await bot.stop()


async def test_private_cost_quota_requires_prices_and_limits(tmp_path):
    bot = await make_bot(tmp_path, builtin=False, llm={
        **LLM,
        "private_daily_cost_limit": 0.01,
        "default": "a",
        "fallback_providers": ["b"],
        "providers": {"a": {"input_price": 1, "output_price": 1}, "b": {}},
    })
    try:
        with pytest.raises(LLMError, match="正数"):
            await bot.chat.complete(MESSAGES, scene="private", scene_id=30003, self_id=10001)
        bot.config_store.patch({"llm": {"providers": {"b": {"input_price": 10000, "output_price": 10000}}}})
        with pytest.raises(LLMError, match="私聊费用额度不足"):
            await bot.chat.complete(MESSAGES, scene="private", scene_id=30003, self_id=10001)
    finally:
        await bot.stop()


async def test_agent_private_quota_exhaustion_makes_no_model_request(tmp_path):
    bot = await make_bot(tmp_path, llm={**LLM, "private_daily_token_limit": 1000})
    try:
        agent = bot.plugins.get("agent")
        await agent.settings.update({
            "private_enabled": True, "cooldown_seconds": 0, "private_allowed_tools": []})
        await bot.storage.record_llm_usage(
            scene="private", scene_id=30003, self_id=10001, provider="p", model="m",
            prompt_tokens=900, completion_tokens=50, latency_ms=1.0,
        )
        captured: list[httpx.Request] = []
        bot.chat.client._http = mock_transport(captured, tokens=15)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(private_message("你好", message_id=11)), connection)
        assert captured == [] and not connection.sent
        turns = (await bot.storage.list_turns(scene="private", scene_id=30003))["items"]
        assert len(turns) == 1
        detail = await bot.storage.turn_detail(turns[0]["turn_id"])
        assert (detail["status"], detail["reason"]) == ("failed", "model_failed")
        assert detail["model_requests"] == []
    finally:
        await bot.stop()
