from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.llm.client import LLMClient, LLMError
from fllbot.storage.db import MIGRATIONS, SCHEMA_VERSION, Storage
from helpers import build_bot, make_bot

AUTH = {'Authorization': 'Bearer admin-token'}
CONFIG = {'enabled': True, 'api_key': 'test', 'model': 'usage-model', 'context_mode': 'custom'}


async def test_upgrade_preserves_old_usage_and_marks_unknown_estimates(tmp_path: Path):
    import sqlite3
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as connection:
        for version in range(1, 5):
            for statement in MIGRATIONS[version]:
                connection.execute(statement)
        connection.execute('PRAGMA user_version = 4')
        connection.execute("INSERT INTO llm_usage (model, prompt_tokens, completion_tokens, created_at) VALUES ('old-model', 20, 10, 1)")
    storage = Storage(path)
    await storage.connect()
    try:
        summary = await storage.llm_usage_summary(0)
        assert summary['total_tokens'] == 30
        assert summary['estimated_requests'] == 1
        assert await storage.schema_version() == SCHEMA_VERSION
    finally:
        await storage.close()


@pytest.mark.parametrize('reported', [False, True])
async def test_usage_estimation_is_explicit_and_zero_is_preserved(tmp_path: Path, reported):
    bot = await make_bot(tmp_path, builtin=False, llm=CONFIG)
    def handler(request):
        payload = {'choices': [{'message': {'content': '你好'}}]}
        if reported:
            payload['usage'] = {'prompt_tokens': 0, 'completion_tokens': 0}
        return httpx.Response(200, json=payload)
    bot.chat.client = LLMClient(lambda: bot.config.llm, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await bot.chat.test('你好')
        summary = await bot.storage.llm_usage_summary(0)
        assert summary['estimated_requests'] == (0 if reported else 1)
        assert (summary['total_tokens'] == 0) is reported
        assert summary['by_model'][0]['estimated_requests'] == (0 if reported else 1)
    finally:
        await bot.stop()


async def test_failed_test_call_is_counted_under_the_configured_model(tmp_path: Path):
    bot = await make_bot(tmp_path, builtin=False, llm=CONFIG)
    bot.chat.client = LLMClient(lambda: bot.config.llm, http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401, json={'error': {'message': 'denied'}}))))
    try:
        with pytest.raises(LLMError):
            await bot.chat.test('test')
        summary = await bot.storage.llm_usage_summary(0)
        assert summary['requests'] == summary['failures'] == 1
        assert summary['total_tokens'] == 0
        assert summary['by_model'][0]['model'] == 'usage-model'
        assert summary['by_model'][0]['provider'] == 'default'
    finally:
        await bot.stop()


def test_usage_is_persisted(tmp_path: Path):
    bot = build_bot(tmp_path, builtin=False, llm=CONFIG)
    with TestClient(create_app(bot)) as client:
        # Use management requests to generate real usage records without external traffic.
        bot.chat.client = LLMClient(lambda: bot.config.llm, http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            'model': 'usage-model', 'choices': [{'message': {'content': 'ok'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
        }))))
        assert client.post('/api/llm/test', headers=AUTH, json={'prompt': 'test'}).status_code == 200
    with TestClient(create_app(build_bot(tmp_path, builtin=False, llm=CONFIG))) as client:
        assert client.get('/api/llm/usage?range=all').status_code == 401
        usage = client.get('/api/llm/usage?range=all', headers=AUTH).json()
        assert usage['requests'] == 1
        assert usage['prompt_tokens'] == 10
        assert usage['completion_tokens'] == 5
        assert usage['estimated_requests'] == 0
