"""生成一组情绪对比样音，并拼成合辑。"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

URL = "http://127.0.0.1:7862"
BASE = Path(__file__).resolve().parent
OUT = BASE / "samples" / "emotion"
OUT.mkdir(parents=True, exist_ok=True)

VOICE = "01-软糯"
LINE = "今天也辛苦啦，要不要我陪你聊一会儿？"
EMOTIONS = [
    ("natural", "自然"),
    ("happy", "开心"),
    ("sad", "难过"),
    ("angry", "生气"),
    ("calm", "平静"),
]


def synth(emotion: str) -> tuple[Path, float]:
    body = json.dumps(
        {"text": LINE, "voice": VOICE, "emotion": emotion}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        URL + "/tts", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = resp.read()
    path = OUT / f"{emotion}.wav"
    path.write_bytes(data)
    info = sf.info(str(path))
    return path, info.duration


pieces = []
for emotion, _ in EMOTIONS:
    path, duration = synth(emotion)
    audio, sr = sf.read(str(path), dtype="float32")
    pieces.append(audio)
    print(f"{emotion}: {duration:.2f}s")

gap = np.zeros(int(sr * 0.8), dtype=np.float32)
combined = []
for piece in pieces:
    combined.extend([piece, gap])
sf.write(BASE / "samples" / "01-软糯-情绪五连.wav", np.concatenate(combined), sr)
print("combined ok")
