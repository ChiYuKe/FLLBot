"""Qwen3-TTS VoiceDesign 本地服务。

和 IndexTTS 服务保持同一套请求字段，插件可以无缝切换后端：
    POST /tts  {text, voice, emotion, emotion_text, duration_factor}

voice 可以是内置预设名（01-软糯 … 10-微沙），也可以直接写一段音色描述。
音色由文字描述固定，不依赖参考音频，因此每次生成都稳定。
"""

from __future__ import annotations

import argparse
import base64
import gc
import hashlib
import json
import logging
import os
import re
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
MODEL_DIR = BASE / "models" / "Base"
VOICE_DIR = BASE / "voices"
CACHE_DIR = BASE / "cache"
IDLE_MARK = "模型已空闲卸载，调用时自动加载"
log = logging.getLogger("qwen-tts-server")

# 高频开场白：启动后在后台预生成，之后直接命中缓存
PREWARM_PHRASES = ("我在。", "怎么啦？", "好呀。", "晚安。", "早上好。")
_WS = re.compile(r"[\s\u3000\u200b-\u200d\ufeff]+")


def normalize_text(text: str) -> str:
    """只做不会改变断句的规范化，保证缓存键稳定。"""
    return _WS.sub(" ", text).strip()

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


class BatchRequest(BaseModel):
    items: list[TTSRequest] = Field(..., min_length=1, max_length=8)


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

    def voice_ref(self, voice: str, emotion: str | None = None) -> tuple[Path, str | None]:
        """返回音色对应的固定参考音频；有情绪参考时优先用情绪版本。"""
        base = (VOICE_DIR / f"{voice}.wav").resolve()
        path = base
        if emotion and emotion != "natural":
            candidate = (VOICE_DIR / "emotions" / voice / f"{emotion}.wav").resolve()
            if candidate.exists() and candidate.is_relative_to(VOICE_DIR.resolve()):
                path = candidate
        if not path.is_relative_to(VOICE_DIR.resolve()) or not path.exists():
            names = "、".join(item["name"] for item in self.list_voices())
            raise HTTPException(400, f"音色没有参考音频：{voice}。可用：{names or '无'}")
        text_path = path.with_suffix(".txt")
        ref_text = text_path.read_text(encoding="utf-8").strip() if text_path.exists() else None
        return path, ref_text

    def list_voices(self) -> list[dict]:
        items = []
        for name in PRESETS:
            path = VOICE_DIR / f"{name}.wav"
            if path.exists():
                items.append({"name": name, "file": str(path), "description": PRESETS[name]})
        return items

    def cache_path(self, req: TTSRequest, ref: Path) -> Path:
        stat = ref.stat()
        src = json.dumps(
            {
                "text": req.text,
                "voice": req.voice,
                "ref": [str(ref), stat.st_size, int(stat.st_mtime)],
                "duration_factor": req.duration_factor,
                "model": "Qwen3-TTS-Base-clone",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(src.encode("utf-8")).hexdigest()[:32]
        return CACHE_DIR / f"{digest}.wav"

    def synthesize(self, req: TTSRequest) -> tuple[bytes, bool]:
        text = normalize_text(req.text)
        if not text:
            raise HTTPException(400, "文本为空")
        ref, ref_text = self.voice_ref(req.voice, req.emotion)
        out = self.cache_path(req, ref)
        # 命中缓存时不需要模型，空闲卸载后依然可以出音频。
        if out.exists() and out.stat().st_size > 1024:
            return out.read_bytes(), True

        with self.lock:
            self.ensure_loaded()
            if not self.ready:
                raise HTTPException(503, self.error or "模型加载失败")
            self.last_used = time.time()
            self.calls += 1
            wavs, sr = self.model.generate_voice_clone(
                text=text,
                language="Chinese",
                ref_audio=str(ref),
                ref_text=ref_text,
                x_vector_only_mode=ref_text is None,
            )
        sf.write(out, wavs[0], sr)

        tempo = 1.0 / max(0.5, min(2.0, req.duration_factor))
        if abs(tempo - 1.0) > 0.01:
            self.apply_tempo(out, tempo)
        return out.read_bytes(), False

    def synthesize_batch(self, reqs: list[TTSRequest]) -> list[tuple[bytes, bool]]:
        """一次批量合成多段，摊薄每步的解码开销。"""
        prepared: list[tuple[TTSRequest, str, Path, str | None, Path]] = []
        for req in reqs:
            text = normalize_text(req.text)
            if not text:
                raise HTTPException(400, "文本为空")
            ref, ref_text = self.voice_ref(req.voice, req.emotion)
            prepared.append((req, text, ref, ref_text, self.cache_path(req, ref)))

        results: list[tuple[bytes, bool] | None] = [None] * len(prepared)
        pending: list[int] = []
        for index, (_, _, _, _, path) in enumerate(prepared):
            if path.exists() and path.stat().st_size > 1024:
                results[index] = (path.read_bytes(), True)
            else:
                pending.append(index)
        if not pending:
            return results  # type: ignore[return-value]

        with self.lock:
            self.ensure_loaded()
            if not self.ready:
                raise HTTPException(503, self.error or "模型加载失败")
            self.last_used = time.time()
            self.calls += len(pending)
            texts = [prepared[i][1] for i in pending]
            refs = [str(prepared[i][2]) for i in pending]
            ref_texts = [prepared[i][3] for i in pending]
            try:
                wavs, sr = self.model.generate_voice_clone(
                    text=texts,
                    language=["Chinese"] * len(texts),
                    ref_audio=refs,
                    ref_text=ref_texts,
                    x_vector_only_mode=[item is None for item in ref_texts],
                )
            except (torch.cuda.OutOfMemoryError, RuntimeError) as exc:
                if "out of memory" not in str(exc).lower():
                    raise
                log.warning("批量显存不足，退回逐条合成：%s", exc)
                wavs, sr = [], 0
                for text, ref, ref_text in zip(texts, refs, ref_texts, strict=False):
                    single, sr = self.model.generate_voice_clone(
                        text=text,
                        language="Chinese",
                        ref_audio=ref,
                        ref_text=ref_text,
                        x_vector_only_mode=ref_text is None,
                    )
                    wavs.extend(single)

        for index, wav in zip(pending, wavs, strict=False):
            req, _, _, _, path = prepared[index]
            sf.write(path, wav, sr)
            tempo = 1.0 / max(0.5, min(2.0, req.duration_factor))
            if abs(tempo - 1.0) > 0.01:
                self.apply_tempo(path, tempo)
            results[index] = (path.read_bytes(), False)
        return results  # type: ignore[return-value]

    def prewarm(self, voices: list[str]) -> None:
        """把高频开场白预先合成到缓存，消息进来时直接命中。"""
        for voice in voices:
            for phrase in PREWARM_PHRASES:
                try:
                    _, cached = self.synthesize(TTSRequest(text=phrase, voice=voice))
                    log.info("预热 %s / %s：%s", voice, phrase, "已有缓存" if cached else "已生成")
                except Exception:  # noqa: BLE001
                    log.exception("预热失败：%s / %s", voice, phrase)

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
    app = FastAPI(title="Qwen3-TTS Base clone local server", version="1.0")

    @app.get("/health")
    async def health() -> dict:
        return {
            "ok": engine.ready,
            "state": engine.state,
            "error": engine.error,
            "device": "cuda:0" if engine.ready else None,
            "model": "Qwen3-TTS-1.7B-Base (clone)",
            "load_seconds": engine.load_seconds,
            "calls": engine.calls,
            "idle_seconds": round(time.time() - engine.last_used, 1) if engine.ready else None,
            "idle_timeout": engine.idle_timeout,
            "uptime": round(time.time() - engine.started_at, 1),
            "voices": len(engine.list_voices()),
        }

    @app.get("/voices")
    async def voices() -> dict:
        return {
            "voices": [
                {"name": item["name"], "description": item["description"]}
                for item in engine.list_voices()
            ]
        }

    @app.post("/tts")
    async def tts(req: TTSRequest) -> Response:
        import anyio

        audio, cached = await anyio.to_thread.run_sync(lambda: engine.synthesize(req))
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={"X-Cache": "hit" if cached else "miss"},
        )

    @app.post("/tts/batch")
    async def tts_batch(req: BatchRequest) -> dict:
        import anyio

        results = await anyio.to_thread.run_sync(lambda: engine.synthesize_batch(req.items))
        return {
            "items": [
                {"audio_b64": base64.b64encode(audio).decode("ascii"), "cached": cached}
                for audio, cached in results
            ]
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Qwen3-TTS VoiceDesign 服务")
    parser.add_argument("--host", default=os.environ.get("QWEN_TTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("QWEN_TTS_PORT", "7862")))
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=float(os.environ.get("QWEN_TTS_IDLE_TIMEOUT", "0")),
        help="空闲多少秒后卸载模型释放显存；0 表示常驻（默认 0）",
    )
    parser.add_argument(
        "--prewarm-voices",
        default=os.environ.get("QWEN_TTS_PREWARM_VOICES", "07-温柔"),
        help="启动后预热哪些音色的高频开场白，逗号分隔；留空则不预热",
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
                    ref, ref_text = engine.voice_ref("01-软糯")
                    engine.model.generate_voice_clone(
                        text="你好。",
                        language="Chinese",
                        ref_audio=str(ref),
                        ref_text=ref_text,
                        x_vector_only_mode=ref_text is None,
                    )
                log.info("预热完成")
            except Exception:  # noqa: BLE001
                log.exception("预热失败（不影响使用）")
        if engine.ready:
            voices = [v.strip() for v in str(args.prewarm_voices).split(",") if v.strip()]
            engine.prewarm(voices)

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
