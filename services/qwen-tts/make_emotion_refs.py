"""为每个预设音色生成多套情绪参考，按说话人相似度择优。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel
from resemblyzer import VoiceEncoder, preprocess_wav

BASE = Path(__file__).resolve().parent
MODEL_DIR = BASE / "models" / "VoiceDesign"
VOICES = BASE / "voices"
OUT = VOICES / "emotions"
OUT.mkdir(parents=True, exist_ok=True)

REF_TEXT = "你好呀，今天也要开心一点，我一直都在这里陪着你。"
CANDIDATES = 3
EMOTIONS = {"happy": "开心", "sad": "难过", "angry": "生气", "calm": "平静"}

PRESETS: dict[str, str] = {
    "01-软糯": "软糯甜美的少女音，音调偏高，语速偏慢，句尾轻轻上扬，像在撒娇",
    "02-元气": "清亮活泼的元气萝莉音，音调高，语速稍快，咬字轻快有弹性",
    "03-气声": "气声很重的少女音，贴耳低语，音量偏小，气息柔软，音调偏高",
    "04-奶音": "奶音少女，咬字偏软，带一点鼻音，句尾黏黏的",
    "05-傲娇": "清脆的傲娇少女音，语速中等，尾音短促，带一点点不耐烦",
    "06-慵懒": "慵懒软萌的少女音，语速慢，尾音拖长，像刚睡醒",
    "07-温柔": "甜而不腻的少女音，中低音区，温柔平稳，气息均匀",
    "08-小恶魔": "小恶魔系少女音，语速快，句尾俏皮上挑，带笑意",
    "09-安静": "安静温柔的少女音，气息轻，语速慢，几乎没有攻击性",
    "10-微沙": "带一点沙哑的可爱少女音，音调偏低，有磁性，说话松弛",
}


def cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def main() -> None:
    only = sys.argv[1:] or None
    model = Qwen3TTSModel.from_pretrained(str(MODEL_DIR), device_map="cuda:0", dtype=torch.bfloat16)
    encoder = VoiceEncoder()
    for name, desc in PRESETS.items():
        if only and name not in only:
            continue
        base = VOICES / f"{name}.wav"
        if not base.exists():
            print("缺少基准参考，跳过", name)
            continue
        base_emb = encoder.embed_utterance(preprocess_wav(base))
        folder = OUT / name
        folder.mkdir(parents=True, exist_ok=True)
        for emotion, cn in EMOTIONS.items():
            audio = folder / f"{emotion}.wav"
            text = folder / f"{emotion}.txt"
            if audio.exists() and text.exists():
                print("skip", name, emotion)
                continue
            best_score, best_wav, best_sr = -1.0, None, 24000
            for _ in range(CANDIDATES):
                wavs, sr = model.generate_voice_design(
                    text=REF_TEXT, language="Chinese", instruct=f"{desc}。语气：非常{cn}"
                )
                emb = encoder.embed_utterance(preprocess_wav(wavs[0], source_sr=sr))
                score = cos(base_emb, emb)
                if score > best_score:
                    best_score, best_wav, best_sr = score, wavs[0], sr
            sf.write(audio, best_wav, best_sr)
            text.write_text(REF_TEXT, encoding="utf-8")
            print(f"{name} {emotion}: 相似度 {best_score:.3f}")


if __name__ == "__main__":
    main()
