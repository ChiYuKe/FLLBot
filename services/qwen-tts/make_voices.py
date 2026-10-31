"""批量生成不同萝莉声线：Qwen3-TTS VoiceDesign。

每一条用同一句话 + 不同音色描述，方便横向对比。
"""

from __future__ import annotations

import sys
from pathlib import Path

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

BASE = Path(__file__).resolve().parent
MODEL_DIR = BASE / "models" / "VoiceDesign"
OUT = BASE / "samples"

TEXT = "你回来啦，我等你好久了，今天想听我说点什么呀？"

VOICES: list[tuple[str, str]] = [
    ("01-软糯", "软糯甜美的少女音，音调偏高，语速偏慢，句尾轻轻上扬，像在撒娇"),
    ("02-元气", "清亮活泼的元气萝莉音，音调高，语速稍快，咬字轻快有弹性"),
    ("03-气声", "气声很重的少女音，贴耳低语，音量偏小，气息柔软"),
    ("04-奶音", "奶音少女，咬字偏软，带一点鼻音，句尾黏黏的"),
    ("05-傲娇", "清脆的傲娇少女音，语速中等，尾音短促，带一点点不耐烦"),
    ("06-慵懒", "慵懒软萌的少女音，语速慢，尾音拖长，像刚睡醒"),
    ("07-温柔", "甜而不腻的少女音，中低音区，温柔平稳，气息均匀"),
    ("08-小恶魔", "小恶魔系少女音，语速快，句尾俏皮上挑，带笑意"),
    ("09-安静", "安静温柔的少女音，气息轻，语速慢，几乎没有攻击性"),
    ("10-微沙", "带一点沙哑的可爱少女音，音调偏低，有磁性，说话松弛"),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    model = Qwen3TTSModel.from_pretrained(
        str(MODEL_DIR),
        device_map="cuda:0",
        dtype=torch.bfloat16,
    )
    only = sys.argv[1:] or None
    for name, desc in VOICES:
        if only and name not in only:
            continue
        target = OUT / f"{name}.wav"
        if target.exists():
            print("skip", name)
            continue
        wavs, sr = model.generate_voice_design(
            text=TEXT,
            language="Chinese",
            instruct=desc,
        )
        sf.write(target, wavs[0], sr)
        print("saved", target.name, f"{len(wavs[0]) / sr:.2f}s")


if __name__ == "__main__":
    main()
