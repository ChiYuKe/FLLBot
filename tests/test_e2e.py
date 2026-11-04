"""端到端测试：真实 uvicorn 服务 + 模拟 NapCat 的 WebSocket 客户端。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

from fllbot.app import create_app
from helpers import build_bot, group_message, lifecycle


async def start_server(app, tmp_path: Path) -> tuple[uvicorn.Server, asyncio.Task, int]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started, "uvicorn 未能启动"
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, port


def _text_of(payload: dict) -> str:
    return "".join(
        segment.get("data", {}).get("text", "")
        for segment in payload["params"]["message"]
        if segment.get("type") == "text"
    )


async def identify_bot(ws, self_id: int = 10001) -> None:
    payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert payload["action"] == "get_login_info"
    await ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": payload["echo"],
                              "data": {"user_id": self_id, "nickname": "木鱼"}}))


@pytest.mark.parametrize('confirmed', [True, False])
async def test_websocket_agent_roundtrip_links_account_turn_echo_and_receipt(tmp_path, confirmed):
    bot = build_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake', 'context_window': 8192},
                    overrides={'chat': {'reply_mode': 'split', 'reply_interval_ms': 0}})
    server, task, port = await start_server(create_app(bot), tmp_path)
    requests = []

    def model(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'wire answer'}, 'finish_reason': 'stop'}],
                                        'usage': {'prompt_tokens': 20, 'completion_tokens': 5}})

    bot.chat.client._http = httpx.AsyncClient(transport=httpx.MockTransport(model))
    try:
        await bot.permissions.save_group(20002, enabled=True, config={'policy': {
            'auto_group': True, 'trigger_modes': ['all'], 'cooldown_seconds': 0}})
        base = f'ws://127.0.0.1:{port}/onebot/v11/ws?access_token=test-token&self_id='
        async with websockets.connect(base + '10001') as first, websockets.connect(base + '10002') as second:
            await identify_bot(first, 10001)
            await identify_bot(second, 10002)
            for account, ws in [(10001, first), (10002, second)]:
                payload = group_message(f'WIRE_INPUT_{account}', message_id=77)
                payload['self_id'] = account
                await ws.send(json.dumps(payload))
                if account == 10001:
                    await ws.send(json.dumps(payload))
                outbound = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert outbound['action'] == 'send_group_msg' and outbound['params']['group_id'] == 20002
                assert _text_of(outbound) == 'wire answer'
                # An unrelated echo must not acknowledge the pending protocol action.
                await ws.send(json.dumps({'status': 'ok', 'retcode': 0, 'echo': 'unknown', 'data': {'message_id': 999}}))
                data = {'message_id': account * 10} if account == 10001 or confirmed else {}
                await ws.send(json.dumps({'status': 'ok', 'retcode': 0, 'echo': outbound['echo'], 'data': data}))
            await asyncio.wait_for(bot.dispatcher._queue.join(), 5)
            await bot.traces.flush()
            assert len(requests) == 2
            events = (await bot.storage.list_message_events())['items']
            assert len(events) == 2
            for event in events:
                account = event['self_id']
                sent = account == 10001 or confirmed
                detail = await bot.storage.turn_detail(event['turn_id'])
                assert detail['status'] == ('delivered' if sent else 'delivery_uncertain')
                assert (detail['trace_metrics']['first_delivery_ms'] is not None) == sent
                assert detail['trace_id'] == event['trace_id'] and detail['trace_id']
                assert len(detail['model_requests']) == 1
                rows = await (await bot.storage.db.execute(
                    'SELECT p.status,p.message_id FROM delivery_parts p JOIN reply_deliveries d USING(delivery_id) '
                    'WHERE d.turn_id=?', (event['turn_id'],))).fetchall()
                assert [tuple(row) for row in rows] == [('sent', str(account * 10)) if sent else ('uncertain', '')]
                history = await bot.storage.recent_chat('group', 20002, self_id=account)
                assert [row.content for row in history if row.role == 'assistant'] == (['wire answer'] if sent else [])
            for ws in (first, second):
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(ws.recv(), 0.05)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        await bot.stop()


async def test_full_message_roundtrip(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=True)
    app = create_app(bot)
    server, task, port = await start_server(app, tmp_path)
    url = f"ws://127.0.0.1:{port}/onebot/v11/ws?access_token=test-token"

    try:
        async with websockets.connect(url) as ws:
            await identify_bot(ws)
            await ws.send(json.dumps(lifecycle()))

            await ws.send(json.dumps(group_message("/ping")))
            payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert payload["action"] == "send_group_msg"
            assert payload["params"]["group_id"] == 20002
            assert "pong" in _text_of(payload)
            assert bot.bots.nickname(10001) == "木鱼"
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 1}, "echo": payload["echo"]}))

            await ws.send(json.dumps(group_message("/help", message_id=2)))
            help_payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert help_payload["action"] == "send_group_msg"
            help_text = _text_of(help_payload)
            assert "可用命令" in help_text
            assert "/echo" in help_text
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 2}, "echo": help_payload["echo"]}))

        assert bot.manager.count() == 0
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        await bot.stop()


async def test_rejects_wrong_token(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=False)
    app = create_app(bot)
    server, task, port = await start_server(app, tmp_path)
    url = f"ws://127.0.0.1:{port}/onebot/v11/ws?access_token=wrong"

    try:
        with pytest.raises(ConnectionClosed):
            async with websockets.connect(url) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        await bot.stop()


async def test_accepts_arbitrary_path(tmp_path: Path) -> None:
    bot = build_bot(tmp_path, builtin=True)
    app = create_app(bot)
    server, task, port = await start_server(app, tmp_path)
    url = f"ws://127.0.0.1:{port}/any/other/path?token=test-token"

    try:
        async with websockets.connect(url) as ws:
            await identify_bot(ws)
            await ws.send(json.dumps(group_message("/echo 复读测试", message_id=3)))
            payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert payload["action"] == "send_group_msg"
            assert "复读测试" in _text_of(payload)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        await bot.stop()
