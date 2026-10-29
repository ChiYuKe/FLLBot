"""模拟 NapCat 的 WebSocket 客户端，用于本地联调。

用法::

    .venv\\Scripts\\python.exe tools\\fake_napcat.py --token <NapCat 里配置的同款 Token>

然后直接输入文字即可模拟群消息；以 ``p `` 开头发私聊，``at `` 开头表示 @机器人。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

try:
    import websockets
except ImportError:  # pragma: no cover
    print("需要 websockets：pip install websockets", file=sys.stderr)
    raise SystemExit(1) from None


class FakeNapCat:
    def __init__(self, url: str, *, self_id: int, group_id: int, user_id: int, nickname: str) -> None:
        self.url = url
        self.self_id = self_id
        self.group_id = group_id
        self.user_id = user_id
        self.nickname = nickname
        self.ws = None
        self._message_id = 1000

    def _next_id(self) -> int:
        self._message_id += 1
        return self._message_id

    async def run(self) -> None:
        async with websockets.connect(self.url) as ws:
            self.ws = ws
            print(f"已连接 {self.url}")
            await self._send(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                    "time": int(time.time()),
                    "self_id": self.self_id,
                }
            )
            reader = asyncio.create_task(self._reader(), name="reader")
            heartbeat = asyncio.create_task(self._heartbeat(), name="heartbeat")
            try:
                await self._console()
            finally:
                reader.cancel()
                heartbeat.cancel()

    async def _send(self, payload: dict) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps(payload, ensure_ascii=False))

    async def _reader(self) -> None:
        assert self.ws is not None
        try:
            async for raw in self.ws:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "action" in payload:
                    await self._on_action(payload)
                else:
                    print(f"<- 事件 {json.dumps(payload, ensure_ascii=False)[:200]}")
        except Exception as exc:  # pragma: no cover
            print(f"连接中断：{exc}")

    async def _on_action(self, payload: dict) -> None:
        action = payload.get("action", "")
        params = payload.get("params") or {}
        print(f"\n<< {action} {json.dumps(params, ensure_ascii=False)[:300]}")

        data: object = {"message_id": self._next_id()}
        if action == "get_login_info":
            data = {"user_id": self.self_id, "nickname": self.nickname}
        elif action == "get_group_list":
            data = [{"group_id": self.group_id, "group_name": "测试群", "member_count": 3}]
        elif action == "get_group_member_info":
            data = {
                "group_id": self.group_id,
                "user_id": params.get("user_id"),
                "nickname": "测试成员",
                "card": "",
                "role": "member",
            }
        elif action == "get_friend_list":
            data = [{"user_id": self.user_id, "nickname": self.nickname}]
        elif action == "get_status":
            data = {"online": True, "good": True}

        await self._send({"status": "ok", "retcode": 0, "data": data, "echo": payload.get("echo")})
        print(">> 已响应")

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self._send(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "time": int(time.time()),
                    "self_id": self.self_id,
                    "interval": 5000,
                    "status": {"online": True, "good": True},
                }
            )

    async def _console(self) -> None:
        print(
            "\n输入内容后回车，模拟群消息：\n"
            "  /help                 直接发命令\n"
            "  at 你好               群消息并 @机器人\n"
            "  p 你好                私聊消息\n"
            "  q                     退出\n"
        )
        loop = asyncio.get_running_loop()
        while True:
            line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
            if not line:
                if not sys.stdin.isatty():
                    return
                continue
            if line in {"q", "quit", "exit"}:
                return
            private = False
            at = False
            text = line
            if line.startswith("p "):
                private, text = True, line[2:]
            elif line.startswith("at "):
                at, text = True, line[3:]
            await self.send_message(text, private=private, at=at)

    async def send_message(self, text: str, *, private: bool = False, at: bool = False) -> None:
        message: list[dict] = []
        if at:
            message.append({"type": "at", "data": {"qq": str(self.self_id)}})
        message.append({"type": "text", "data": {"text": text}})
        payload: dict = {
            "time": int(time.time()),
            "self_id": self.self_id,
            "post_type": "message",
            "message_type": "private" if private else "group",
            "sub_type": "friend" if private else "normal",
            "message_id": self._next_id(),
            "user_id": self.user_id,
            "raw_message": text,
            "font": 0,
            "message": message,
            "sender": {"user_id": self.user_id, "nickname": self.nickname, "card": "", "role": "member"},
        }
        if not private:
            payload["group_id"] = self.group_id
        await self._send(payload)
        print(f"-> {'私聊' if private else '群'}: {text}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模拟 NapCat 反向 WebSocket 客户端")
    parser.add_argument("--url", default="ws://127.0.0.1:6701/onebot/v11/ws")
    parser.add_argument("--token", default="", help="与 server.access_token 一致")
    parser.add_argument("--self-id", type=int, default=10001)
    parser.add_argument("--group-id", type=int, default=20002)
    parser.add_argument("--user-id", type=int, default=30003)
    parser.add_argument("--nickname", default="测试用户")
    args = parser.parse_args(argv)

    url = args.url
    if args.token:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}access_token={args.token}"

    client = FakeNapCat(
        url,
        self_id=args.self_id,
        group_id=args.group_id,
        user_id=args.user_id,
        nickname=args.nickname,
    )
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
