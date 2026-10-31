# Plugin: voice | Role: implementation | Metadata: plugin.toml
"""语音插件：把 bot 想说的话用 IndexTTS-2.5 念出来。

依赖本地 TTS 服务（services/tts/server.py），插件本身只做 HTTP 客户端：
    /say 今天好累啊           → 合成并发送语音
    /say [生气] 你再说一遍    → 指定情绪
    /voices                   → 查看可用音色

也可以被智能体当作工具调用（插件名__speak）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path

import httpx

from fllbot.onebot.segments import MessageSegment
from fllbot.plugin import Context, Plugin

log = logging.getLogger(__name__)

plugin = Plugin.from_manifest(__file__)

# 不给用户看的默认值；表达能力交给智能体在每次调用时决定
LANG = "zh"
MAX_CHARS = 120
MAX_CHUNKS = 8
PAUSE_MS = 200
TIMEOUT = 180.0
DEFAULT_ALPHA = 0.9
INDEXTTS_SERVER = "http://127.0.0.1:7861"
QWEN_SERVER = "http://127.0.0.1:7862"
INDEXTTS_FX = "warm"

QWEN_VOICES = (
    "01-软糯", "02-元气", "03-气声", "04-奶音", "05-傲娇",
    "06-慵懒", "07-温柔", "08-小恶魔", "09-安静", "10-微沙",
)

plugin.setting("enabled", type="bool", label="启用语音", default=True, group_scoped=True)
plugin.setting(
    "backend",
    type="select",
    label="语音后端",
    default="qwen",
    options=("qwen", "indextts"),
    description="qwen：描述固定、稳定不漂；indextts：用参考音频克隆",
)
plugin.setting(
    "qwen_voice",
    type="select",
    label="Qwen 音色",
    default="04-奶音",
    options=QWEN_VOICES,
    description="试听页里选中的编号",
    group_scoped=True,
)
plugin.setting(
    "tts_voice",
    type="string",
    label="IndexTTS 音色",
    default="dongxuelian-soft",
    description="仅 indextts 后端使用，对应 services/tts/voices 里的文件名",
    group_scoped=True,
)
plugin.setting("with_text", type="bool", label="同时发送文字", default=True, group_scoped=True)
plugin.setting(
    "use_emotion",
    type="bool",
    label="允许情绪控制",
    default=True,
    group_scoped=True,
    description="关闭后固定按音色描述生成，忽略智能体和 /say 传入的语气，音色最稳定",
)

# 情绪前缀： [生气] xxx / （开心）xxx / 开心: xxx
_BRACKET_EMOTION = re.compile(r"^\s*[\[\(【（]\s*([^\]\)】）]{1,8}?)\s*[\]\)】）]\s*")
_COLON_EMOTION = re.compile(r"^\s*([\u4e00-\u9fa5]{1,4})\s*[:：]\s*")

_EMOTION_ALIASES = {
    "开心": "happy", "高兴": "happy", "快活": "happy",
    "生气": "angry", "愤怒": "angry", "恼火": "angry",
    "难过": "sad", "悲伤": "sad", "伤心": "sad",
    "害怕": "afraid", "恐惧": "afraid",
    "厌恶": "disgusted", "反感": "disgusted",
    "低落": "melancholic", "郁闷": "melancholic",
    "惊讶": "surprised", "吃惊": "surprised",
    "平静": "calm", "冷静": "calm", "自然": "natural", "普通": "natural",
}

_URL = re.compile(r"https?://\S+")
_CODE_FENCE = re.compile(r"```.*?```", re.S)
_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,8}\]")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u200D]+"
)


def _clean(text: str) -> str:
    text = _CODE_FENCE.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _PLACEHOLDER.sub(" ", text)
    text = _EMOJI.sub("", text)
    text = re.sub(r"[*_~`>#|]", "", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return text.strip()


def _parse_emotion(text: str) -> tuple[str | None, str]:
    for pattern in (_BRACKET_EMOTION, _COLON_EMOTION):
        match = pattern.match(text)
        if not match:
            continue
        word = match.group(1).strip()
        emotion = _EMOTION_ALIASES.get(word, word.lower())
        if emotion in _EMOTION_ALIASES.values():
            return emotion, text[match.end():]
    return None, text


_SOFT_BREAK = "，、；：,;:"


def _split_long_sentence(sentence: str, limit: int, hard_max: int) -> list[str]:
    """超长句优先在标点处切；没有标点才硬切，并补逗号收尾，避免突然断气。"""
    parts: list[str] = []
    while len(sentence) > hard_max:
        window = sentence[:hard_max]
        cut = max(window.rfind(p) for p in _SOFT_BREAK)
        if cut >= limit // 2:
            parts.append(sentence[:cut].strip())
        else:
            cut = hard_max
            parts.append(sentence[:cut].rstrip() + "，")
        sentence = sentence[cut:].lstrip(_SOFT_BREAK + " ")
    if sentence.strip():
        parts.append(sentence.strip())
    return parts


def _split_text(text: str, limit: int) -> list[str]:
    """按句切分；一句话不超过 hard_max 就整句保留，避免从词中间断开。"""
    hard_max = max(limit, 220)
    sentences = [s for s in re.split(r"(?<=[。！？!?…；;\n])", text) if s.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) <= limit:
            current += sentence
            continue
        if current:
            chunks.append(current.strip())
            current = ""
        if len(sentence) <= hard_max:
            current = sentence
            continue
        parts = _split_long_sentence(sentence, limit, hard_max)
        chunks.extend(parts[:-1])
        current = parts[-1]
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c][:MAX_CHUNKS]


def _data_dir(ctx: Context) -> Path:
    try:
        root = Path(ctx.config.storage.path).parent
    except Exception:  # noqa: BLE001
        root = Path("data")
    path = root / "tts"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


async def _request_tts(
    ctx: Context,
    text: str,
    emotion: str | None,
    emotion_text: str | None = None,
    emo_alpha: float | None = None,
    speed: float | None = None,
    settings: dict | None = None,
) -> Path:
    settings = settings or _snapshot(ctx)
    if not settings.get("use_emotion", True):
        emotion = None
        emotion_text = None
        emo_alpha = None
    backend = str(settings.get("backend") or "qwen")
    if backend == "indextts":
        url = INDEXTTS_SERVER + "/tts"
        voice = settings.get("tts_voice")
        payload = {
            "text": text,
            "voice": voice,
            "lang": LANG,
            "emotion": emotion or "natural",
            "emotion_text": emotion_text,
            "emo_alpha": float(emo_alpha if emo_alpha is not None else DEFAULT_ALPHA),
            "duration_factor": 1.0 / max(0.5, float(speed if speed is not None else 1.0)),
            "interval_silence": PAUSE_MS,
            "fx": INDEXTTS_FX,
        }
    else:
        url = QWEN_SERVER + "/tts"
        voice = settings.get("qwen_voice")
        payload = {
            "text": text,
            "voice": voice,
            "emotion": emotion or "natural",
            "emotion_text": emotion_text,
            "emo_alpha": float(emo_alpha if emo_alpha is not None else DEFAULT_ALPHA),
            "duration_factor": 1.0 / max(0.5, float(speed if speed is not None else 1.0)),
        }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        log.info(
            "TTS 请求：backend=%s voice=%s emotion=%s emotion_text=%s speed=%s text=%r",
            backend, voice, payload.get("emotion"), emotion_text, speed, text[:40],
        )
        response = await client.post(url, json=payload)
        response.raise_for_status()
        audio = response.content

    digest = hashlib.sha256(
        (text + backend + str(voice) + str(payload.get("emotion")) + str(emotion_text)).encode("utf-8")
    ).hexdigest()[:24]
    path = _data_dir(ctx) / f"{digest}.wav"
    path.write_bytes(audio)
    return path


_SETTING_KEYS = ("enabled", "backend", "qwen_voice", "tts_voice", "with_text", "use_emotion")
_BACKGROUND: set[asyncio.Task] = set()


def _snapshot(ctx: Context) -> dict:
    """在处理器返回前把配置固化下来，后台任务不再依赖 ctx.plugin。"""
    return {key: ctx.settings.get(key) for key in _SETTING_KEYS}


def _launch(coro) -> None:
    """语音发送放后台跑，避免长文本把 agent 处理器拖到超时。"""
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def _send(
    ctx: Context,
    text: str,
    emotion: str | None,
    emotion_text: str | None = None,
    emo_alpha: float | None = None,
    speed: float | None = None,
    settings: dict | None = None,
) -> None:
    settings = settings or _snapshot(ctx)
    if not settings.get("enabled"):
        await ctx.reply("语音功能未启用")
        return

    clean = _clean(text)
    if not clean:
        await ctx.reply("要念什么？用法：/say 想说的话（可加 [开心] 前缀）")
        return

    chunks = _split_text(clean, MAX_CHARS)
    if len(chunks) > 1:
        log.info("文本 %s 字，拆成 %s 段语音", len(clean), len(chunks))
    if len(clean) > MAX_CHARS * MAX_CHUNKS:
        log.warning("文本超过上限（%s 字），只发送前 %s 段", MAX_CHARS * MAX_CHUNKS, MAX_CHUNKS)

    for index, chunk in enumerate(chunks):
        try:
            path = await _request_tts(ctx, chunk, emotion, emotion_text, emo_alpha, speed, settings)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:200]
            log.warning("TTS 服务返回错误：%s %s", exc.response.status_code, detail)
            if index == 0:
                await ctx.reply(f"语音生成失败（{exc.response.status_code}）：{detail}")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("语音生成失败")
            if index == 0:
                await ctx.reply(f"语音生成失败：{type(exc).__name__} {exc}")
            return

        segments = []
        if settings.get("with_text") and index == 0:
            segments.append(MessageSegment.text(chunk))
        segments.append(MessageSegment.record(path.as_uri()))
        await ctx.reply(segments)
        if index + 1 < len(chunks):
            await asyncio.sleep(0.4)


@plugin.on_command("say", aliases=("说", "tts", "念"), cooldown=8.0)
async def say(ctx: Context) -> None:
    """/say [情绪] 文本 —— 合成语音"""
    text = (ctx.args or "").strip()
    if not text:
        await ctx.reply("用法：/say [情绪] 想说的话\n情绪可选：开心、生气、难过、害怕、厌恶、低落、惊讶、平静")
        return
    emotion, body = _parse_emotion(text)
    settings = _snapshot(ctx)
    log.info("收到 /say，转入后台生成：%s", (body or text)[:40])
    _launch(_send(ctx, body or text, emotion, settings=settings))


def _backend(ctx: Context) -> tuple[str, str]:
    """返回 (后端名, 服务地址)。"""
    backend = str(ctx.settings.get("backend") or "qwen")
    if backend == "indextts":
        return "indextts", INDEXTTS_SERVER
    return "qwen", QWEN_SERVER


def _current_voice(ctx: Context, backend: str) -> str:
    return str(ctx.settings.get("tts_voice" if backend == "indextts" else "qwen_voice"))


@plugin.on_command("voices", aliases=("音色",))
async def voices(ctx: Context) -> None:
    """列出可用音色"""
    backend, server = _backend(ctx)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            data = (await client.get(server + "/voices")).json()
    except Exception as exc:  # noqa: BLE001
        await ctx.reply(f"TTS 服务不可用：{type(exc).__name__} {exc}")
        return
    items = data.get("voices") or []
    if not items:
        await ctx.reply("当前后端没有可用音色")
        return
    names = "、".join(item["name"] for item in items)
    await ctx.reply(f"后端：{backend}\n可用音色：{names}\n当前：{_current_voice(ctx, backend)}")


@plugin.on_command("ttshealth", aliases=("语音状态",))
async def health(ctx: Context) -> None:
    """检查 TTS 服务状态"""
    backend, server = _backend(ctx)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            data = (await client.get(server + "/health")).json()
    except Exception as exc:  # noqa: BLE001
        await ctx.reply(f"连不上 {backend} 服务：{type(exc).__name__} {exc}")
        return
    state = str(data.get("state") or ("ready" if data.get("ok") else "loading"))
    label = {"ready": "就绪", "idle": "空闲（已卸载，调用时自动加载）", "loading": "加载中"}.get(state, "未就绪")
    timeout = data.get("idle_timeout")
    idle_note = f"，空闲 {data.get('idle_seconds')}s / 卸载阈值 {timeout}s" if state == "ready" and timeout else ""
    await ctx.reply(
        f"后端：{backend}｜模型：{label}{idle_note}\n"
        f"设备：{data.get('device')}｜音色：{data.get('voices')} 个｜调用：{data.get('calls')}\n"
        f"当前音色：{_current_voice(ctx, backend)}\n"
        f"错误：{data.get('error') or '无'}"
    )


@plugin.on_ai_tool(
    "speak",
    title="语音朗读",
    description=(
        "把一段文字用语音念出来并发送到当前会话。用户要求“用语音说/念出来/发条语音”时调用。"
        "可以一次传入完整的长文本（故事、说明、多段回复都可以），插件会按句自动拆成多条语音逐条发送，"
        "所以不要为了缩短语音而总结、删减或改写原文，用户要听的就是你原本要说的话。"
        "调用前根据上下文判断该用什么语气：道谢、夸奖用 happy；被冒犯、警告用 angry；"
        "安慰、遗憾用 sad 或 melancholic；平静叙述用 natural。"
        "如果不确定该选哪一档，或需要更细腻的语气（比如“嘴上嫌弃但有点开心”），用 emotion_text 自由描述，它会覆盖 emotion。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要念的完整文本；长故事直接整段传入，插件负责分段"},
            "emotion": {
                "type": "string",
                "enum": ["natural", "happy", "angry", "sad", "afraid", "melancholic", "surprised", "calm"],
                "description": "预设语气，默认 natural；有 emotion_text 时以 emotion_text 为准",
            },
            "emotion_text": {
                "type": "string",
                "description": "自由描述语气，例如“温柔地低声说，带一点撒娇”“强装镇定但有点委屈”，中文即可",
            },
            "emo_alpha": {
                "type": "number",
                "description": "情绪强度 0–1；不传则用 0.9，太高会让声音飘",
            },
            "speed": {
                "type": "number",
                "description": "语速，1.0 正常，0.9 慢一点更柔和，1.1 快一点；不传则 1.0",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
)
async def speak(
    ctx: Context,
    text: str,
    emotion: str = "natural",
    emotion_text: str | None = None,
    emo_alpha: float | None = None,
    speed: float | None = None,
) -> dict:
    settings = _snapshot(ctx)
    log.info("工具调用 voice__speak，转入后台生成：%s", text[:40])
    _launch(_send(ctx, text, emotion, emotion_text, emo_alpha, speed, settings=settings))
    return {"ok": True, "silent": True, "queued": True, "spoken": text, "emotion": emotion_text or emotion}
