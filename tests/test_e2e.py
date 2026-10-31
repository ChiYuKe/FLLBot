"""端到端测试：真实 uvicorn 服务 + 模拟 NapCat 的 WebSocket 客户端。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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


async def identify_bot(ws) -> None:
    payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert payload["action"] == "get_login_info"
    await ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": payload["echo"],
                              "data": {"user_id": 10001, "nickname": "木鱼"}}))


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
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {}, "echo": help_payload["echo"]}))

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
