"""对已经运行的 FLLBot 做一次冒烟：WS 收发 + 管理 API。

先启动服务::

    .venv\\Scripts\\python.exe -m fllbot run --root .

再执行::

    .venv\\Scripts\\python.exe tools\\smoke_test.py

会自动从 data/admin_token.txt 读取管理令牌（也可用 --token 指定）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import websockets
except ImportError:  # pragma: no cover
    print("需要 websockets：pip install websockets", file=sys.stderr)
    raise SystemExit(1) from None


async def check_ws(url: str, *, self_id: int, group_id: int, timeout: float = 10.0) -> bool:
    async with websockets.connect(url) as ws:
        await ws.send(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                    "time": 0,
                    "self_id": self_id,
                }
            )
        )
        message = {
            "time": 0,
            "self_id": self_id,
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": 1,
            "group_id": group_id,
            "user_id": 30003,
            "raw_message": "/ping",
            "font": 0,
            "message": [{"type": "text", "data": {"text": "/ping"}}],
            "sender": {"user_id": 30003, "nickname": "冒烟测试", "role": "member"},
        }
        await ws.send(json.dumps(message))
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        payload = json.loads(raw)
        action = payload.get("action")
        text = "".join(
            segment.get("data", {}).get("text", "")
            for segment in (payload.get("params") or {}).get("message", [])
            if segment.get("type") == "text"
        )
        print(f"[WS] action={action} text={text!r}")
        if action != "send_group_msg" or "pong" not in text:
            print("[WS] 未收到预期的 pong 回复", file=sys.stderr)
            return False
        await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 1}, "echo": payload.get("echo")}))
        return True


def check_api(base: str, token: str) -> bool:
    ok = True
    for path in ("/api/health", "/api/status", "/api/plugins"):
        request = urllib.request.Request(f"{base}{path}")
        if path != "/api/health" and token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
                preview = json.dumps(body, ensure_ascii=False)[:120]
                print(f"[API] {path} -> {response.status} {preview}")
        except urllib.error.HTTPError as exc:
            print(f"[API] {path} -> {exc.code} {exc.reason}", file=sys.stderr)
            ok = False
        except urllib.error.URLError as exc:
            print(f"[API] {path} 连接失败：{exc.reason}", file=sys.stderr)
            ok = False
    return ok


def _configure_stdio() -> None:
    """Windows 控制台编码可能不支持特殊字符，避免打印时崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # pragma: no cover
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="FLLBot 冒烟测试")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6701)
    parser.add_argument("--path", default="/onebot/v11/ws")
    parser.add_argument("--ws-token", default="")
    parser.add_argument("--token", default="", help="管理令牌，默认读 data/admin_token.txt")
    parser.add_argument("--self-id", type=int, default=10001)
    parser.add_argument("--group-id", type=int, default=20002)
    args = parser.parse_args(argv)

    base = f"http://{args.host}:{args.port}"
    ws_url = f"ws://{args.host}:{args.port}{args.path}"
    if args.ws_token:
        ws_url += f"?access_token={args.ws_token}"

    token = args.token
    if not token:
        token_file = Path("data/admin_token.txt")
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()

    api_ok = check_api(base, token)
    try:
        ws_ok = asyncio.run(check_ws(ws_url, self_id=args.self_id, group_id=args.group_id))
    except Exception as exc:
        print(f"[WS] 失败：{exc}", file=sys.stderr)
        ws_ok = False

    if api_ok and ws_ok:
        print("\n冒烟测试通过")
        return 0
    print("\n冒烟测试失败", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
