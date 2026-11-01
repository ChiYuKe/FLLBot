"""为十个音色各生成一组情绪样音与合辑。"""

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

VOICES = [
    "01-软糯", "02-元气", "03-气声", "04-奶音", "05-傲娇",
    "06-慵懒", "07-温柔", "08-小恶魔", "09-安静", "10-微沙",
]
LINE = "今天也辛苦啦，要不要我陪你聊一会儿？"
EMOTIONS = ["natural", "happy", "sad", "angry", "calm"]


def synth(voice: str, emotion: str, path: Path) -> float:
    if path.exists() and path.stat().st_size > 1024:
        return sf.info(str(path)).duration
    body = json.dumps({"text": LINE, "voice": voice, "emotion": emotion}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        URL + "/tts", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        path.write_bytes(resp.read())
    return sf.info(str(path)).duration


rows = []
for voice in VOICES:
    folder = OUT / voice
    folder.mkdir(parents=True, exist_ok=True)
    pieces = []
    durations = {}
    sr = 24000
    for emotion in EMOTIONS:
        path = folder / f"{emotion}.wav"
        durations[emotion] = synth(voice, emotion, path)
        audio, sr = sf.read(str(path), dtype="float32")
        pieces.append(audio)
    gap = np.zeros(int(sr * 0.8), dtype=np.float32)
    combined = []
    for piece in pieces:
        combined.extend([piece, gap])
    sf.write(folder / "combined.wav", np.concatenate(combined), sr)
    rows.append((voice, durations))
    print(voice, " ".join(f"{e}={d:.2f}s" for e, d in durations.items()))

print("done", len(rows))
