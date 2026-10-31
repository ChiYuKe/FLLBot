"""Qwen3-TTS VoiceDesign 本地服务。

和 IndexTTS 服务保持同一套请求字段，插件可以无缝切换后端：
    POST /tts  {text, voice, emotion, emotion_text, duration_factor}

voice 可以是内置预设名（01-软糯 … 10-微沙），也可以直接写一段音色描述。
音色由文字描述固定，不依赖参考音频，因此每次生成都稳定。
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from qwen_tts import Qwen3TTSModel

BASE = Path(__file__).resolve().parent
MODEL_DIR = BASE / "models" / "VoiceDesign"
CACHE_DIR = BASE / "cache"
IDLE_MARK = "模型已空闲卸载，调用时自动加载"
log = logging.getLogger("qwen-tts-server")

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

EMOTION_CN = {
    "happy": "开心",
    "angry": "生气",
    "sad": "难过",
    "afraid": "害怕",
    "disgusted": "厌恶",
    "melancholic": "低落",
    "surprised": "惊讶",
    "calm": "平静",
}


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=400)
    voice: str = "01-软糯"
    emotion: str | None = None
    emotion_text: str | None = None
    emo_alpha: float = 1.0
    duration_factor: float = 1.0
    lang: str = "zh"
    fx: str | None = None  # 兼容旧客户端，Qwen 后端不使用
    interval_silence: int | None = None
    speed: float | None = None


class Engine:
    def __init__(self) -> None:
        self.model: Qwen3TTSModel | None = None
        self.error: str | None = None
        self.lock = threading.Lock()
        self.load_lock = threading.Lock()
        self.calls = 0
        self.started_at = time.time()
        self.load_seconds: float | None = None
        # 空闲超过 idle_timeout 秒后释放显存，下次请求再加载；0 表示一直常驻。
        self.idle_timeout = 900.0
        self.last_used = time.time()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def ready(self) -> bool:
        return self.model is not None

    @property
    def state(self) -> str:
        if self.ready:
            return "ready"
        if self.error == IDLE_MARK:
            return "idle"
        return "loading"

    def load(self) -> None:
        begun = time.time()
        try:
            self.model = Qwen3TTSModel.from_pretrained(
                str(MODEL_DIR),
                device_map="cuda:0",
                dtype=torch.bfloat16,
            )
            self.error = None
            self.last_used = time.time()
            self.load_seconds = round(time.time() - begun, 1)
            log.info("模型加载完成，用时 %.1fs", self.load_seconds)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("模型加载失败")

    def ensure_loaded(self) -> None:
        """按需加载；调用方需持有 self.lock，避免与空闲卸载交错。"""
        if self.ready:
            return
        with self.load_lock:
            if not self.ready:
                self.load()

    def unload_locked(self) -> None:
        """释放模型；调用方需持有 self.lock。"""
        if self.model is None:
            return
        self.model = None
        self.error = IDLE_MARK
        gc.collect()
        try:
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        except Exception:  # noqa: BLE001
            log.debug("清理显存缓存失败", exc_info=True)

    def instruct_for(self, req: TTSRequest) -> str:
        base = PRESETS.get(req.voice, req.voice)
        if not base.strip():
            base = PRESETS["01-软糯"]
        if req.emotion_text:
            return f"{base}。语气：{req.emotion_text}"
        if req.emotion and req.emotion != "natural":
            cn = EMOTION_CN.get(req.emotion)
            if cn:
                level = "非常" if req.emo_alpha >= 0.85 else ("比较" if req.emo_alpha >= 0.6 else "稍微")
                return f"{base}。语气：{level}{cn}"
        return base

    def cache_path(self, req: TTSRequest, instruct: str) -> Path:
        src = json.dumps(
            {
                "text": req.text,
                "instruct": instruct,
                "duration_factor": req.duration_factor,
                "model": "Qwen3-TTS-VoiceDesign",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(src.encode("utf-8")).hexdigest()[:32]
        return CACHE_DIR / f"{digest}.wav"

    def synthesize(self, req: TTSRequest) -> tuple[bytes, bool]:
        text = " ".join(req.text.split())
        if not text:
            raise HTTPException(400, "文本为空")
        instruct = self.instruct_for(req)
        out = self.cache_path(req, instruct)
        # 命中缓存时不需要模型，空闲卸载后依然可以出音频。
        if out.exists() and out.stat().st_size > 1024:
            return out.read_bytes(), True

        with self.lock:
            self.ensure_loaded()
            if not self.ready:
                raise HTTPException(503, self.error or "模型加载失败")
            self.last_used = time.time()
            self.calls += 1
            wavs, sr = self.model.generate_voice_design(
                text=text,
                language="Chinese",
                instruct=instruct,
            )
        sf.write(out, wavs[0], sr)

        tempo = 1.0 / max(0.5, min(2.0, req.duration_factor))
        if abs(tempo - 1.0) > 0.01:
            self.apply_tempo(out, tempo)
        return out.read_bytes(), False

    def apply_tempo(self, path: Path, tempo: float) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return
        tmp = path.with_suffix(".tmp.wav")
        result = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-af", f"atempo={max(0.8, min(1.25, tempo)):.4f}", "-ar", "24000", "-ac", "1", str(tmp)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and tmp.exists():
            os.replace(tmp, path)
        else:
            log.warning("变速失败：%s", result.stderr[-200:])
            tmp.unlink(missing_ok=True)


engine = Engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Qwen3-TTS VoiceDesign local server", version="1.0")

    @app.get("/health")
    async def health() -> dict:
        return {
            "ok": engine.ready,
            "state": engine.state,
            "error": engine.error,
            "device": "cuda:0" if engine.ready else None,
            "model": "Qwen3-TTS-1.7B-VoiceDesign",
            "load_seconds": engine.load_seconds,
            "calls": engine.calls,
            "idle_seconds": round(time.time() - engine.last_used, 1) if engine.ready else None,
            "idle_timeout": engine.idle_timeout,
            "uptime": round(time.time() - engine.started_at, 1),
            "voices": len(PRESETS),
        }

    @app.get("/voices")
    async def voices() -> dict:
        return {"voices": [{"name": name, "description": desc} for name, desc in PRESETS.items()]}

    @app.post("/tts")
    async def tts(req: TTSRequest) -> Response:
        import anyio

        audio, cached = await anyio.to_thread.run_sync(lambda: engine.synthesize(req))
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={"X-Cache": "hit" if cached else "miss"},
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Qwen3-TTS VoiceDesign 服务")
    parser.add_argument("--host", default=os.environ.get("QWEN_TTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("QWEN_TTS_PORT", "7862")))
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=float(os.environ.get("QWEN_TTS_IDLE_TIMEOUT", "900")),
        help="空闲多少秒后卸载模型释放显存；0 表示常驻（默认 900）",
    )
    parser.add_argument("--no-warmup", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine.idle_timeout = max(0.0, args.idle_timeout)

    def bootstrap() -> None:
        engine.ensure_loaded()
        if engine.ready and not args.no_warmup:
            try:
                with engine.lock:
                    engine.model.generate_voice_design(
                        text="你好。", language="Chinese", instruct=PRESETS["01-软糯"]
                    )
                log.info("预热完成")
            except Exception:  # noqa: BLE001
                log.exception("预热失败（不影响使用）")

    def idle_watch(interval: float = 15.0) -> None:
        while True:
            time.sleep(interval)
            try:
                timeout = engine.idle_timeout
                if timeout <= 0 or not engine.ready:
                    continue
                if time.time() - engine.last_used < timeout:
                    continue
                if not engine.lock.acquire(timeout=5):
                    continue
                try:
                    if engine.ready and time.time() - engine.last_used >= timeout:
                        idle = time.time() - engine.last_used
                        engine.unload_locked()
                        log.info("空闲 %.0f 秒，已卸载模型释放显存（下次调用自动加载）", idle)
                finally:
                    engine.lock.release()
            except Exception:  # noqa: BLE001
                log.exception("空闲卸载检查失败")

    threading.Thread(target=bootstrap, name="qwen-loader", daemon=True).start()
    threading.Thread(target=idle_watch, name="qwen-idle-watch", daemon=True).start()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
