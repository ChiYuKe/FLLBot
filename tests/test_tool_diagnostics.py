"""工具执行统计：存储聚合口径、管理 API 鉴权/参数与隐私边界。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.webui import render_index
from helpers import build_bot

AUTH = {"Authorization": "Bearer admin-token"}
ARGUMENT_SENTINEL = 'PRIVATE-ARGUMENT-SENTINEL'
RESULT_SENTINEL = 'PRIVATE-TOOL-RESULT-SENTINEL'


async def _insert_execution(
    storage,
    *,
    execution_id: str,
    self_id: int,
    tool: str,
    status: str,
    created_at: float,
    elapsed_ms: float | None = None,
    error: str = '',
    arguments_hash: str = '',
) -> None:
    finished_at = None if elapsed_ms is None else created_at + elapsed_ms / 1000
    await storage.db.execute(
        "INSERT INTO tool_executions(execution_id,self_id,scene,scene_id,message_id,tool,effect,status,"
        "created_at,finished_at,error,turn_id,call_id,arguments_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (execution_id, self_id, 'group', 123, 0, tool, 'read_only', status, created_at, finished_at,
         error, '', '', arguments_hash),
    )
    await storage.db.commit()


async def _seed_executions(storage, now: float) -> None:
    # Two web calls (one success, one timeout) and two workspace calls (uncertain + blocked/cancelled).
    await _insert_execution(storage, execution_id='e1', self_id=10001, tool='web__read_webpage',
                            status='succeeded', created_at=now - 3600, elapsed_ms=250, arguments_hash=ARGUMENT_SENTINEL)
    await _insert_execution(storage, execution_id='e2', self_id=10001, tool='web__read_webpage',
                            status='failed', created_at=now - 1800, elapsed_ms=500, error='TimeoutError')
    await _insert_execution(storage, execution_id='e3', self_id=10001, tool='workspace__read_file',
                            status='uncertain', created_at=now - 7200, elapsed_ms=1000, error='CancelledError')
    await _insert_execution(storage, execution_id='e4', self_id=10001, tool='workspace__read_file',
                            status='cancelled', created_at=now - 300, elapsed_ms=100)
    await _insert_execution(storage, execution_id='e5', self_id=10002, tool='screen__capture_screen',
                            status='succeeded', created_at=now - 600, elapsed_ms=2000)
    await _insert_execution(storage, execution_id='e6', self_id=10001, tool='agent__search_history',
                            status='running', created_at=now - 60)
    await _insert_execution(storage, execution_id='e7', self_id=10001, tool='old__tool',
                            status='succeeded', created_at=now - 8 * 86400, elapsed_ms=100)
    await _insert_execution(storage, execution_id='e8', self_id=10001, tool='mid__tool',
                            status='failed', created_at=now - 2 * 86400, elapsed_ms=400, error='RuntimeError')
    await storage.db.execute("INSERT OR REPLACE INTO tool_results VALUES(?,?,?)", ('e1', RESULT_SENTINEL, 0))
    await storage.db.commit()


def test_console_bundle_exposes_tool_stats_panel() -> None:
    html = render_index()
    assert 'data-page="tool-stats"' in html
    assert 'loadToolStats' in html
    for identifier in ('tool-stats-refresh', 'tool-stats-filter', 'tool-stats-days', 'tool-stats-bot',
                       'tool-stats-note', 'tool-stats-body'):
        assert f'id="{identifier}"' in html


async def test_tool_execution_stats_aggregates_statuses_and_window(tmp_path: Path) -> None:
    now = time.time()
    bot = build_bot(tmp_path, builtin=False)
    await bot.storage.connect()
    try:
        empty = await bot.storage.tool_execution_stats()
        assert empty['window']['days'] == 7 and empty['window']['self_id'] is None
        assert abs(empty['window']['since'] - (now - 7 * 86400)) < 5
        assert empty['items'] == []
        assert empty['totals'] == {'tools': 0, 'calls': 0, 'succeeded': 0, 'failed': 0,
                                   'uncertain': 0, 'blocked': 0, 'running': 0}
        await _seed_executions(bot.storage, now)
        result = await bot.storage.tool_execution_stats()
        assert result['window']['days'] == 7 and result['window']['self_id'] is None
        assert abs(result['window']['since'] - (now - 7 * 86400)) < 5
        items = {item['tool']: item for item in result['items']}
        assert list(items) == ['web__read_webpage', 'workspace__read_file', 'agent__search_history',
                               'mid__tool', 'screen__capture_screen']
        assert 'old__tool' not in items
        web = items['web__read_webpage']
        assert (web['calls'], web['succeeded'], web['failed'], web['uncertain'], web['blocked'], web['running']) == (
            2, 1, 1, 0, 0, 0)
        assert web['avg_ms'] == 375.0 and web['last_error'] == 'TimeoutError' and web['last_at'] == now - 1800
        workspace = items['workspace__read_file']
        assert (workspace['calls'], workspace['uncertain'], workspace['blocked']) == (2, 1, 1)
        assert workspace['avg_ms'] == 550.0 and workspace['last_error'] == 'CancelledError'
        assert workspace['last_at'] == now - 300
        running = items['agent__search_history']
        assert running['running'] == 1 and running['avg_ms'] is None and running['last_error'] is None
        assert items['mid__tool']['avg_ms'] == 400.0 and items['mid__tool']['last_error'] == 'RuntimeError'
        assert result['totals'] == {'tools': 5, 'calls': 7, 'succeeded': 2, 'failed': 2, 'uncertain': 1,
                                    'blocked': 1, 'running': 1}
        assert set(web) == {'tool', 'calls', 'succeeded', 'failed', 'uncertain', 'blocked', 'running',
                            'avg_ms', 'last_error', 'last_at'}
        serialized = json.dumps(result)
        assert ARGUMENT_SENTINEL not in serialized and RESULT_SENTINEL not in serialized
        assert 'arguments' not in serialized and 'content' not in serialized
    finally:
        await bot.storage.close()


async def test_tool_execution_stats_filters_self_id_and_bounds_days(tmp_path: Path) -> None:
    now = time.time()
    bot = build_bot(tmp_path, builtin=False)
    await bot.storage.connect()
    try:
        await _seed_executions(bot.storage, now)
        own = await bot.storage.tool_execution_stats(self_id=10001)
        assert [item['tool'] for item in own['items']] == ['web__read_webpage', 'workspace__read_file',
                                                          'agent__search_history', 'mid__tool']
        assert own['window']['self_id'] == 10001 and own['totals']['calls'] == 6
        other = await bot.storage.tool_execution_stats(self_id=10002)
        assert [item['tool'] for item in other['items']] == ['screen__capture_screen']
        assert other['totals']['calls'] == 1
        recent = await bot.storage.tool_execution_stats(days=1, self_id=10001)
        assert recent['window']['days'] == 1 and 'mid__tool' not in {item['tool'] for item in recent['items']}
        assert (await bot.storage.tool_execution_stats(days=0))['window']['days'] == 1
        assert (await bot.storage.tool_execution_stats(days=-5))['window']['days'] == 1
        assert (await bot.storage.tool_execution_stats(days=1000))['window']['days'] == 90
        assert (await bot.storage.tool_execution_stats(days='invalid'))['window']['days'] == 7
    finally:
        await bot.storage.close()


def test_tool_stats_api_auth_validation_structure_and_privacy(tmp_path: Path) -> None:
    bot = build_bot(tmp_path)
    now = time.time()
    with TestClient(create_app(bot)) as client:
        assert client.portal is not None
        client.portal.call(_seed_executions, bot.storage, now)
        assert client.get('/api/console/tools/stats').status_code == 401
        response = client.get('/api/console/tools/stats', headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data['window']['days'] == 7 and data['window']['self_id'] is None
        assert data['totals']['calls'] == 7 and len(data['items']) == 5
        assert set(data['items'][0]) == {'tool', 'calls', 'succeeded', 'failed', 'uncertain', 'blocked',
                                         'running', 'avg_ms', 'last_error', 'last_at'}
        filtered = client.get('/api/console/tools/stats?days=30&self_id=10002', headers=AUTH).json()
        assert [item['tool'] for item in filtered['items']] == ['screen__capture_screen']
        assert filtered['window']['self_id'] == 10002 and filtered['window']['days'] == 30
        empty = client.get('/api/console/tools/stats?self_id=99999', headers=AUTH).json()
        assert empty['items'] == [] and empty['totals']['calls'] == 0 and empty['totals']['tools'] == 0
        for query in ('days=0', 'days=91', 'days=-1', 'days=abc', 'self_id=0', 'self_id=-2'):
            assert client.get('/api/console/tools/stats?' + query, headers=AUTH).status_code == 422
        text = response.text
        assert ARGUMENT_SENTINEL not in text and RESULT_SENTINEL not in text
        assert 'arguments' not in text and 'message_id' not in text and 'scene_id' not in text
