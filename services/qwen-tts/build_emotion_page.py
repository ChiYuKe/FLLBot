"""计算情绪样音与基准音色的相似度，并生成汇总试听页。"""

from __future__ import annotations

import html
from pathlib import Path

import numpy as np
import soundfile as sf
from resemblyzer import VoiceEncoder, preprocess_wav

BASE = Path(__file__).resolve().parent
OUT = BASE / "samples" / "emotion"
VOICES = [
    "01-软糯", "02-元气", "03-气声", "04-奶音", "05-傲娇",
    "06-慵懒", "07-温柔", "08-小恶魔", "09-安静", "10-微沙",
]
EMOTIONS = [("natural", "自然"), ("happy", "开心"), ("sad", "难过"), ("angry", "生气"), ("calm", "平静")]

encoder = VoiceEncoder()
sections = []
for voice in VOICES:
    ref = BASE / "voices" / f"{voice}.wav"
    ref_emb = encoder.embed_utterance(preprocess_wav(ref))
    items = []
    for emotion, label in EMOTIONS:
        path = OUT / voice / f"{emotion}.wav"
        duration = sf.info(str(path)).duration
        emb = encoder.embed_utterance(preprocess_wav(path))
        sim = float(np.dot(ref_emb, emb) / (np.linalg.norm(ref_emb) * np.linalg.norm(emb)))
        items.append((emotion, label, duration, sim))
    sections.append((voice, items))
    print(voice, " ".join(f"{label}={sim:.3f}" for _, label, _, sim in items))

cards = []
for voice, items in sections:
    players = "\n".join(
        f'''      <div class="row">
        <div class="tag">{label}</div>
        <audio controls preload="none" src="{voice}/{emotion}.wav"></audio>
        <div class="meta">{duration:.2f}s · 相似度 {sim:.2f}</div>
      </div>'''
        for emotion, label, duration, sim in items
    )
    cards.append(
        f'''  <section>
    <h2>{voice}</h2>
    <div class="row combined">
      <div class="tag">连听</div>
      <audio controls preload="none" src="{voice}/combined.wav"></audio>
      <div class="meta">自然→开心→难过→生气→平静</div>
    </div>
{players}
  </section>'''
    )

page = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>十个音色 · 情绪对比</title>
<style>
  :root {{ --bg:#0e1116; --card:#161b23; --line:#242c37; --text:#e6edf3; --muted:#8b98a9; --accent:#f0a6ca; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }}
  header {{ padding:22px 26px 10px; border-bottom:1px solid var(--line); }}
  h1 {{ margin:0 0 6px; font-size:20px; }}
  header p {{ margin:0; color:var(--muted); font-size:12px; }}
  main {{ max-width:900px; margin:0 auto; padding:16px 26px 60px; }}
  section {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin-bottom:12px; }}
  h2 {{ margin:0 0 10px; font-size:16px; color:var(--accent); }}
  .row {{ display:grid; grid-template-columns:52px 1fr 130px; gap:10px; align-items:center; margin-bottom:8px; }}
  .tag {{ font-size:12px; color:var(--muted); }}
  .meta {{ font-size:11px; color:var(--muted); }}
  audio {{ width:100%; height:32px; }}
  .combined .tag {{ color:var(--text); }}
</style>
</head>
<body>
<header>
  <h1>十个音色 · 五种情绪</h1>
  <p>同一句话：今天也辛苦啦，要不要我陪你聊一会儿？ · 每个音色 5 条 + 一条合辑</p>
</header>
<main>
{chr(10).join(cards)}
</main>
</body>
</html>
'''
(OUT / "index.html").write_text(page, encoding="utf-8")
print("index written")
