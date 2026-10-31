"""IndexTTS-2.5 本地 HTTP 服务。

给 FLLBot 的语音插件提供一个稳定的本地接口，隔离 torch / 显存 / 模型加载：

    vendor\\IndexTTS\\.venv\\Scripts\\python.exe server.py --port 7861

接口：
    GET  /health   健康检查（模型是否加载完成）
    GET  /voices   列出 voices/ 下可用的参考音频
    POST /tts      合成为 wav，返回 audio/wav

设计要点：
- 单 GPU 串行推理（线程锁），避免并发把 16G 显存打爆；
- 结果按参数哈希缓存到 cache/，同样的话第二次直接命中；
- 支持音色参考、情绪参考、情绪向量、情绪文本、语速、句间停顿。
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
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
VENDOR = BASE / "vendor" / "IndexTTS"
sys.path.insert(0, str(VENDOR))

log = logging.getLogger("indextts-server")

# 情绪顺序与 IndexTTS-2.5 内部一致：
# [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
EMOTIONS: dict[str, list[float]] = {
    "happy": [1, 0, 0, 0, 0, 0, 0, 0],
    "angry": [0, 1, 0, 0, 0, 0, 0, 0],
    "sad": [0, 0, 1, 0, 0, 0, 0, 0],
    "afraid": [0, 0, 0, 1, 0, 0, 0, 0],
    "disgusted": [0, 0, 0, 0, 1, 0, 0, 0],
    "melancholic": [0, 0, 0, 0, 0, 1, 0, 0],
    "surprised": [0, 0, 0, 0, 0, 0, 1, 0],
    "calm": [0, 0, 0, 0, 0, 0, 0, 1],
    "natural": [],  # 不干预，沿用参考音频的情绪
}

LANGS = {"zh", "en", "ja", "es", "ar"}
IDLE_MARK = "模型已空闲卸载，调用时自动加载"

# 后期预设：让输出更“暖、近、贴耳”。
# 注意 ffmpeg 的 loudnorm 会把采样率抬到 192k，所以最后统一回 22.05k。
_WARM = (
    "highpass=f=70,"
    "equalizer=f=140:width_type=q:w=1.0:g=1.8,"
    "equalizer=f=330:width_type=q:w=1.4:g=-1.5,"
    "equalizer=f=3200:width_type=q:w=1.1:g=1.8,"
    "equalizer=f=10500:width_type=q:w=1.4:g=1.5,"
    "deesser=i=0.35:m=0.5:f=0.5,"
    "acompressor=threshold=-20dB:ratio=2.2:attack=8:release=160:makeup=1"
)
_LOUD = "loudnorm=I=-16:TP=-1.5:LRA=9"
FX_CHAINS = {
    "none": None,
    "warm": f"{_WARM},{_LOUD}",
    "warm-low": f"{_WARM},rubberband=pitch=0.917:tempo=0.97,{_LOUD}",
    "warm-low-space": f"{_WARM},rubberband=pitch=0.917:tempo=0.97,aecho=0.85:0.9:40:0.08,{_LOUD}",
}


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=600)
    voice: str = "default"
    lang: str = "zh"
    emotion: str | None = None
    emotion_text: str | None = None
    emo_audio: str | None = None
    emo_alpha: float = 1.0
    duration_factor: float = 1.0
    interval_silence: int = 200
    fx: str = "warm-low"
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    repetition_penalty: float | None = None


class Engine:
    def __init__(self, checkpoints: Path, voice_dir: Path, cache_dir: Path, *,
                 bf16: bool = True, qwen_emo: bool = True, cuda_kernel: bool | None = None):
        self.checkpoints = checkpoints
        self.voice_dir = voice_dir
        self.cache_dir = cache_dir
        self.bf16 = bf16
        self.qwen_emo = qwen_emo
        self.cuda_kernel = cuda_kernel
        self.tts = None
        self.error: str | None = None
        self.load_seconds: float | None = None
        self.lock = threading.Lock()
        self.load_lock = threading.Lock()
        self.started_at = time.time()
        self.calls = 0
        # 空闲超过 idle_timeout 秒后释放显存，下次请求再加载；0 表示一直常驻。
        self.idle_timeout = 900.0
        self.last_used = time.time()

    # -- 模型 ------------------------------------------------------------- #
    def load(self) -> None:
        begun = time.time()
        try:
            from indextts.infer_v2_5 import IndexTTS2

            cfg = self.checkpoints / "config.yaml"
            if not cfg.exists():
                raise FileNotFoundError(f"找不到模型配置：{cfg}（先下载 IndexTTS-2.5 权重）")
            kwargs = {"use_bf16": self.bf16, "use_qwen_emo": self.qwen_emo}
            if self.cuda_kernel is not None:
                kwargs["use_cuda_kernel"] = self.cuda_kernel
            self.tts = IndexTTS2(cfg_path=str(cfg), model_dir=str(self.checkpoints), **kwargs)
            self.error = None
            self.last_used = time.time()
            self.load_seconds = round(time.time() - begun, 1)
            log.info("模型加载完成，用时 %.1fs", self.load_seconds)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("模型加载失败")

    @property
    def ready(self) -> bool:
        return self.tts is not None

    @property
    def state(self) -> str:
        if self.ready:
            return "ready"
        if self.error == IDLE_MARK:
            return "idle"
        return "loading"

    def ensure_loaded(self) -> None:
        """按需加载；调用方需持有 self.lock，避免与空闲卸载交错。"""
        if self.ready:
            return
        with self.load_lock:
            if not self.ready:
                self.load()

    def unload_locked(self) -> None:
        """释放模型；调用方需持有 self.lock。"""
        if self.tts is None:
            return
        self.tts = None
        self.error = IDLE_MARK
        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        except Exception:  # noqa: BLE001
            log.debug("清理显存缓存失败", exc_info=True)

    def warmup(self) -> None:
        """用一句短文本预热，避免第一条消息慢得离谱。"""
        try:
            voice = self.resolve_voice("default")
            tmp = self.cache_dir / "_warmup.wav"
            with self.lock:
                self.tts.infer(spk_audio_prompt=str(voice), text="你好。", output_path=str(tmp), lang="zh")
            tmp.unlink(missing_ok=True)
            log.info("预热完成")
        except Exception:  # noqa: BLE001
            log.exception("预热失败（不影响使用）")

    # -- 参考音频 --------------------------------------------------------- #
    def _voice_roots(self) -> list[Path]:
        roots = [self.voice_dir, VENDOR / "examples"]
        return [root.resolve() for root in roots if root.exists()]

    def list_voices(self) -> list[dict]:
        found: dict[str, dict] = {}
        for root in self._voice_roots():
            for path in sorted(root.glob("*.wav")):
                name = path.stem
                if name in found:
                    continue
                stat = path.stat()
                found[name] = {"name": name, "file": str(path), "size": stat.st_size,
                               "mtime": int(stat.st_mtime)}
        return list(found.values())

    def resolve_voice(self, voice: str) -> Path:
        # 允许直接传绝对路径，但必须位于允许的目录里
        candidate = Path(voice)
        if candidate.suffix.lower() == ".wav" and candidate.is_absolute():
            resolved = candidate.resolve()
            if any(resolved.is_relative_to(root) for root in self._voice_roots()) and resolved.exists():
                return resolved
            raise HTTPException(400, f"参考音频不在允许目录内：{voice}")

        roots = self._voice_roots()
        for root in roots:
            path = (root / f"{voice}.wav").resolve()
            if path.exists():
                return path
        if voice in {"", "default", "auto"}:
            defaults = [self.voice_dir / "default.wav", VENDOR / "examples" / "voice_01.wav"]
            for path in defaults:
                if path.exists():
                    return path.resolve()
            raise HTTPException(400, "没有可用的参考音频，请把 wav 放到 services/tts/voices/ 下")
        names = "、".join(item["name"] for item in self.list_voices())
        raise HTTPException(400, f"音色不存在：{voice}。可用音色：{names or '无'}")

    # -- 合成 ------------------------------------------------------------- #
    def cache_path(self, payload: dict, voice: Path) -> Path:
        stat = voice.stat()
        key_src = json.dumps(
            {
                "text": payload["text"],
                "voice": [str(voice), stat.st_size, int(stat.st_mtime)],
                "lang": payload["lang"],
                "emotion": payload.get("emotion"),
                "emotion_text": payload.get("emotion_text"),
                "emo_audio": payload.get("emo_audio"),
                "emo_alpha": payload.get("emo_alpha"),
                "duration_factor": payload.get("duration_factor"),
                "interval_silence": payload.get("interval_silence"),
                "fx": payload.get("fx"),
                "temperature": payload.get("temperature"),
                "top_p": payload.get("top_p"),
                "top_k": payload.get("top_k"),
                "repetition_penalty": payload.get("repetition_penalty"),
                "model": "IndexTTS-2.5",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{digest}.wav"

    def synthesize(self, req: TTSRequest) -> tuple[bytes, bool]:
        if req.lang not in LANGS:
            raise HTTPException(400, f"不支持的语言：{req.lang}")

        text = " ".join(req.text.split())
        if not text:
            raise HTTPException(400, "文本为空")

        voice = self.resolve_voice(req.voice)
        payload = req.model_dump()
        payload["text"] = text
        out = self.cache_path(payload, voice)
        if out.exists() and out.stat().st_size > 1024:
            return out.read_bytes(), True

        emo_vector = EMOTIONS.get((req.emotion or "natural").lower())
        if req.emotion and emo_vector is None:
            raise HTTPException(400, f"未知情绪：{req.emotion}，可选：{', '.join(EMOTIONS)}")

        emo_audio = None
        if req.emo_audio:
            emo_audio = self.resolve_voice(req.emo_audio)

        gen_kwargs: dict[str, float | int] = {}
        if req.temperature is not None:
            gen_kwargs["temperature"] = max(0.1, min(1.5, req.temperature))
        if req.top_p is not None:
            gen_kwargs["top_p"] = max(0.1, min(1.0, req.top_p))
        if req.top_k is not None:
            gen_kwargs["top_k"] = max(1, min(100, req.top_k))
        if req.repetition_penalty is not None:
            gen_kwargs["repetition_penalty"] = max(1.0, min(20.0, req.repetition_penalty))

        with self.lock:
            self.ensure_loaded()
            if not self.ready:
                raise HTTPException(503, self.error or "模型加载失败")
            self.last_used = time.time()
            self.calls += 1
            self.tts.infer(
                spk_audio_prompt=str(voice),
                text=text,
                output_path=str(out),
                lang=req.lang,
                emo_audio_prompt=str(emo_audio) if emo_audio else None,
                emo_alpha=max(0.0, min(1.0, req.emo_alpha)),
                emo_vector=emo_vector or None,
                use_emo_text=bool(req.emotion_text),
                emo_text=req.emotion_text,
                interval_silence=max(0, min(2000, req.interval_silence)),
                duration_factor=max(0.5, min(2.0, req.duration_factor)),
                verbose=False,
                **gen_kwargs,
            )
        if not out.exists():
            raise HTTPException(500, "合成失败：没有生成音频")
        self.apply_fx(out, req.fx)
        return out.read_bytes(), False

    def apply_fx(self, path: Path, fx: str) -> None:
        """按预设做后期；失败时保留干声，不影响主流程。"""
        if fx not in FX_CHAINS:
            raise HTTPException(400, f"未知后期预设：{fx}，可选：{', '.join(FX_CHAINS)}")
        chain = FX_CHAINS[fx]
        if not chain:
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            log.warning("找不到 ffmpeg，跳过后期预设 %s", fx)
            return
        tmp = path.with_suffix(".fx.wav")
        result = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-af", chain, "-ar", "22050", "-ac", "1", str(tmp)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not tmp.exists():
            log.warning("后期失败（%s）：%s", fx, result.stderr[-300:])
            tmp.unlink(missing_ok=True)
            return
        os.replace(tmp, path)


engine: Engine | None = None


def create_app() -> FastAPI:
    app = FastAPI(title="IndexTTS-2.5 local server", version="1.0")

    @app.get("/health")
    async def health() -> dict:
        assert engine is not None
        return {
            "ok": engine.ready,
            "state": engine.state,
            "error": engine.error,
            "device": getattr(engine.tts, "device", None),
            "bf16": engine.bf16,
            "qwen_emo": engine.qwen_emo,
            "load_seconds": engine.load_seconds,
            "calls": engine.calls,
            "idle_seconds": round(time.time() - engine.last_used, 1) if engine.ready else None,
            "idle_timeout": engine.idle_timeout,
            "uptime": round(time.time() - engine.started_at, 1),
            "voices": len(engine.list_voices()) if engine.voice_dir.exists() else 0,
        }

    @app.get("/voices")
    async def voices() -> dict:
        assert engine is not None
        return {"voices": engine.list_voices()}

    @app.post("/tts")
    async def tts(req: TTSRequest) -> Response:
        assert engine is not None
        import anyio

        audio, cached = await anyio.to_thread.run_sync(lambda: engine.synthesize(req))
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={"X-Cache": "hit" if cached else "miss"},
        )

    @app.get("/lab", response_class=HTMLResponse)
    async def lab() -> HTMLResponse:
        page = BASE / "lab.html"
        if not page.exists():
            raise HTTPException(404, "找不到 lab.html")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.post("/voices/upload")
    async def upload_voice(file: UploadFile = File(...), name: str = Form("")) -> dict:
        assert engine is not None
        import anyio
        import re

        raw_name = (name or Path(file.filename or "voice").stem).strip()
        safe = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fa5]", "", raw_name)[:32]
        if not safe:
            raise HTTPException(400, "音色名不合法")

        data = await file.read()
        if len(data) > 50 * 1024 * 1024:
            raise HTTPException(413, "文件太大（上限 50MB）")

        def save() -> Path:
            tmp = engine.voice_dir / f".upload-{safe}.tmp"
            tmp.write_bytes(data)
            target = engine.voice_dir / f"{safe}.wav"
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise HTTPException(500, "服务器没有 ffmpeg，无法转码")
            result = subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp),
                 "-vn", "-ac", "1", "-ar", "24000",
                 "-af", "highpass=f=60,lowpass=f=10000,loudnorm=I=-18:TP=-2:LRA=11",
                 str(target)],
                capture_output=True,
                text=True,
            )
            tmp.unlink(missing_ok=True)
            if result.returncode != 0 or not target.exists():
                raise HTTPException(400, f"音频转码失败：{result.stderr[-200:]}")
            return target

        path = await anyio.to_thread.run_sync(save)
        return {"name": path.stem, "file": str(path), "size": path.stat().st_size}

    @app.get("/voices/{name}/audio")
    async def voice_audio(name: str) -> FileResponse:
        assert engine is not None
        path = (engine.voice_dir / f"{name}.wav").resolve()
        if not path.is_relative_to(engine.voice_dir.resolve()) or not path.exists():
            raise HTTPException(404, "音色不存在")
        return FileResponse(path, media_type="audio/wav")

    @app.delete("/voices/{name}")
    async def delete_voice(name: str) -> dict:
        assert engine is not None
        if name == "default":
            raise HTTPException(400, "不能删除 default")
        path = (engine.voice_dir / f"{name}.wav").resolve()
        if not path.is_relative_to(engine.voice_dir.resolve()) or not path.exists():
            raise HTTPException(404, "音色不存在")
        path.unlink()
        return {"ok": True, "name": name}

    return app


def main() -> None:
    global engine
    parser = argparse.ArgumentParser(description="IndexTTS-2.5 HTTP 服务")
    parser.add_argument("--host", default=os.environ.get("INDEXTTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("INDEXTTS_PORT", "7861")))
    parser.add_argument("--checkpoints", default=os.environ.get("INDEXTTS_CHECKPOINTS", str(BASE / "checkpoints")))
    parser.add_argument("--voices", default=os.environ.get("INDEXTTS_VOICES", str(BASE / "voices")))
    parser.add_argument("--cache", default=os.environ.get("INDEXTTS_CACHE", str(BASE / "cache")))
    parser.add_argument("--no-bf16", action="store_true", help="关闭 bf16（显存不够时反而更慢）")
    parser.add_argument("--no-qwen-emo", action="store_true", help="不加载情绪文本模型，省 1-2G 显存")
    parser.add_argument("--cuda-kernel", choices=("on", "off"), default=os.environ.get("INDEXTTS_CUDA_KERNEL"))
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=float(os.environ.get("INDEXTTS_IDLE_TIMEOUT", "900")),
        help="空闲多少秒后卸载模型释放显存；0 表示常驻（默认 900）",
    )
    parser.add_argument("--no-warmup", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cuda_kernel = None if args.cuda_kernel is None else args.cuda_kernel == "on"
    engine = Engine(
        checkpoints=Path(args.checkpoints),
        voice_dir=Path(args.voices),
        cache_dir=Path(args.cache),
        bf16=not args.no_bf16,
        qwen_emo=not args.no_qwen_emo,
        cuda_kernel=cuda_kernel,
    )
    engine.voice_dir.mkdir(parents=True, exist_ok=True)
    engine.cache_dir.mkdir(parents=True, exist_ok=True)
    engine.idle_timeout = max(0.0, args.idle_timeout)

    def bootstrap() -> None:
        engine.ensure_loaded()
        if engine.ready and not args.no_warmup:
            engine.warmup()

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

    # 后台加载模型，先让 /health 能响应（插件和人都能看到“加载中”）
    threading.Thread(target=bootstrap, name="model-loader", daemon=True).start()
    threading.Thread(target=idle_watch, name="model-idle-watch", daemon=True).start()

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
