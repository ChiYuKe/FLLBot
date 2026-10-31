"""TTS 服务冒烟测试：python smoke.py [文本] [情绪]"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:7861"
OUT = Path(__file__).resolve().parent / "cache" / "smoke.wav"


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=15) as response:
        return json.loads(response.read())


def post(payload: dict, timeout: float = 300) -> tuple[bytes, dict]:
    request = urllib.request.Request(
        BASE + "/tts",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), dict(response.headers)


def main() -> None:
    text = sys.argv[1] if len(sys.argv) > 1 else "你好呀，我是浮莲。今天想听我说点什么？"
    emotion = sys.argv[2] if len(sys.argv) > 2 else "natural"
    voice = sys.argv[3] if len(sys.argv) > 3 else "default"

    health = get("/health")
    print("health:", json.dumps(health, ensure_ascii=False))
    print("voices:", json.dumps(get("/voices"), ensure_ascii=False))

    started = time.time()
    audio, headers = post({"text": text, "voice": voice, "emotion": emotion, "lang": "zh"})
    elapsed = time.time() - started
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(audio)
    print(f"ok: {len(audio)} bytes, {elapsed:.2f}s, cache={headers.get('x-cache') or headers.get('X-Cache')}")
    print("saved:", OUT)


if __name__ == "__main__":
    main()
