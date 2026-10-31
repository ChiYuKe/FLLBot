"""为十个预置音色各生成一条带编号的试听，并拼成合集。"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "samples" / "listen"
OUT.mkdir(parents=True, exist_ok=True)
URL = "http://127.0.0.1:7862/tts"
LINE = "你回来啦，我等你好久了，今天想听我说点什么呀？"
VOICES = [
    "01-软糯", "02-元气", "03-气声", "04-奶音", "05-傲娇",
    "06-慵懒", "07-温柔", "08-小恶魔", "09-安静", "10-微沙",
]


def synth(voice: str, index: int) -> Path:
    payload = {"text": f"第{index}号。{LINE}", "voice": voice, "emotion": "natural"}
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    path = OUT / f"{voice}.wav"
    path.write_bytes(data)
    print(f"{voice}: {len(data)/1024:.0f}KB")
    return path


for i, voice in enumerate(VOICES, 1):
    synth(voice, i)

gap = OUT / "_gap.wav"
subprocess.run(
    ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
     "-i", "anullsrc=r=24000:cl=mono", "-t", "0.7", str(gap)],
    check=True,
)
lines = []
for voice in VOICES:
    lines.append(f"file '{voice}.wav'")
    lines.append("file '_gap.wav'")
list_file = OUT / "list.txt"
list_file.write_text("\n".join(lines), encoding="utf-8")
combined = BASE / "samples" / "萝莉声线-十连听-带编号.wav"
subprocess.run(
    ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
     "-safe", "0", "-i", str(list_file), "-c", "copy", str(combined)],
    check=True,
)
gap.unlink(missing_ok=True)
list_file.unlink(missing_ok=True)
duration = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(combined)],
    capture_output=True, text=True,
).stdout.strip()
print("combined:", combined, duration, "s")
