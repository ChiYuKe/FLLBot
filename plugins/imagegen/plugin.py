# Plugin: imagegen | Role: commands and AI tool | Metadata: plugin.toml
"""图片生成：让群友用一句话画出表情包、头像、插画或场景图。

命令和 AI 工具共用同一套生成与发送流程；服务地址、模型和密钥都由管理员在后台配置。
生成结果统一转成 PNG 后以 base64 图片发送，不落盘，避免在工作区留下群友的图片。
"""

from __future__ import annotations

import asyncio
import base64
import math
import time
from typing import Any

from fllbot.plugin import Context, MessageSegment, Plugin, send_media_reply

from . import local
from .service import (
    PROVIDERS,
    ImageGenError,
    generate,
    parse_size,
    validate_engine,
    validate_prompt,
)

PROVIDER_OPTIONS = ("local", *PROVIDERS)

plugin = Plugin.from_manifest(__file__)

plugin.setting(
    "enabled",
    type="bool",
    label="启用图片生成",
    default=True,
    group_scoped=True,
    description="关闭后本群不响应 /画，也不会调用图片服务；不影响其他群。",
)
plugin.setting(
    "provider",
    type="select",
    label="图片服务类型",
    default="local",
    options=PROVIDER_OPTIONS,
    description=(
        "local：用 Pillow 直接画机器人自己的猫娘形象，免密钥、免联网；"
        "openai/siliconflow/sdwebui：远程或本机图片模型，能画任意画面，但需要自行配置服务。"
    ),
)
plugin.setting(
    "base_url",
    type="string",
    label="远程服务地址",
    default="https://api.openai.com/v1",
    description=(
        "仅在非 local 模式使用。openai/siliconflow 填到 /v1 即可，例如 https://api.siliconflow.cn/v1；"
        "sdwebui 填 http://127.0.0.1:7860。"
    ),
)
plugin.setting(
    "api_key",
    type="string",
    label="远程 API Key",
    default="",
    secret=True,
    description="仅在非 local 模式需要；local 模式留空即可。sdwebui 没开 --api-auth 时也留空。",
)
plugin.setting(
    "model",
    type="string",
    label="远程模型名称",
    default="gpt-image-1",
    description="仅在非 local 模式使用，例如 gpt-image-1、dall-e-3、Kwai-Kolors/Kolors；local 忽略。",
)
plugin.setting(
    "size",
    type="select",
    label="默认尺寸",
    default="1024x1024",
    options=("1024x1024", "768x1024", "1024x768", "1024x1536", "1536x1024", "512x512"),
    description="模型支持的范围以服务商为准；工具调用时模型可以另行指定。",
)
plugin.setting(
    "style",
    type="string",
    label="追加风格（远程）",
    default="",
    description="仅远程模式：每次生成都追加在提示词末尾，例如「动漫插画风，柔和逆光」；留空不追加。",
)
plugin.setting(
    "negative_prompt",
    type="string",
    label="默认负面提示词（远程）",
    default="低质量, 模糊, 变形, 多余肢体, 文字水印",
    description="仅远程模式使用；openai 协议没有负面提示词参数。",
)
plugin.setting(
    "steps",
    type="int",
    label="采样步数（远程）",
    default=28,
    min=1,
    max=100,
    description="仅 siliconflow / sdwebui 使用；步数越高越慢。",
)
plugin.setting(
    "guidance",
    type="float",
    label="提示词强度（远程）",
    default=7.5,
    min=1.0,
    max=30.0,
    description="仅远程模式：guidance_scale / cfg_scale，越高越贴提示词，过高会僵硬。",
)
plugin.setting(
    "response_format",
    type="select",
    label="返回格式（远程）",
    default="auto",
    options=("auto", "url", "b64_json"),
    description="仅 openai 协议使用；auto 不传该参数，兼容性最好。",
)
plugin.setting(
    "timeout",
    type="float",
    label="生成超时（秒，远程）",
    default=120.0,
    min=10.0,
    max=240.0,
    description="仅远程模式使用；图片服务较慢时可调高。",
)
plugin.setting(
    "cooldown_seconds",
    type="int",
    label="同一用户冷却（秒）",
    default=5,
    min=0,
    max=600,
    group_scoped=True,
    description="同一用户两次生成之间的最小间隔；本地模式可以调低，远程按次计费建议调高。",
)
plugin.setting(
    "max_image_mb",
    type="float",
    label="图片大小上限（MB）",
    default=8.0,
    min=1.0,
    max=20.0,
    description="超过上限的图片不会发送，避免超长 base64 拖垮发送。",
)
plugin.setting(
    "max_prompt_chars",
    type="int",
    label="提示词上限（字）",
    default=700,
    min=50,
    max=2000,
    description="超过上限时拒绝生成，让模型或群友先精简描述。",
)
plugin.setting(
    "caption_font",
    type="string",
    label="图片文字字体",
    default="",
    description="本地模式画图片上的短文字时使用；留空自动查找系统中文字体（微软雅黑等）。",
)
plugin.agent_instruction(
    "image_generation",
    tools=["draw"],
    text=(
        "群友明确要求画图、生成图片、表情包、头像、插画、壁纸或场景图时调用 draw。"
        "默认的 local 模式是插件用 Pillow 现画的机器人自己（银灰发猫娘、猫耳、黑旗袍），"
        "适合做表情包、头像和情绪图；可以指定 expression（normal/happy/angry/sad/shy/sleepy/"
        "surprised/smug/love/cry/blank）、palette（night/sakura/rain/sunset/mint/mono）、"
        "最 16 字的 caption 和 seed。省略 expression/palette 时会按 prompt 关键词判断。"
        "prompt 用一句中文描述画面或情绪即可，例如“生气地鼓着腮帮子”“雨夜，委屈巴巴”。"
        "local 只能画这个猫娘形象，画不了别的角色、写实照片或复杂构图；"
        "遇到这种请求，如果返回提示说明只能画本喵，就如实告诉群友，不要用文字假装画好。"
        "生成成功后只简短回应，不重复描述画面或提示词；同一请求不要连续生成多张，"
        "群友要求换一张时再调用并换一个 seed。"
        "不要生成违法内容、涉及未成年人的性化内容，或拿真实人物的脸做侮辱性图片。"
        "自动参与（没有被 @ 或唤醒词叫到）时插件不会生成图片；若返回 imagegen_addressed_required，"
        "请让群友明确叫你再画。"
    ),
)

_SLOTS = asyncio.Semaphore(2)
_LAST: dict[tuple[int, int, int], float] = {}


def _engine(settings: Any):
    megabytes = float(settings.get("max_image_mb") or 8)
    return validate_engine(
        provider=settings.get("provider"),
        base_url=settings.get("base_url"),
        api_key=settings.get("api_key"),
        model=settings.get("model"),
        size=settings.get("size"),
        negative_prompt=settings.get("negative_prompt"),
        steps=settings.get("steps"),
        guidance=settings.get("guidance"),
        response_format=settings.get("response_format"),
        timeout=settings.get("timeout"),
        max_bytes=int(megabytes * 1024 * 1024),
    )


def _compose_prompt(prompt: str, style: Any, limit: int) -> str:
    text = validate_prompt(prompt, limit)
    extra = " ".join(str(style or "").split())
    if extra and extra.casefold() not in text.casefold():
        room = max(20, limit - len(extra) - 1)
        text = f"{text[:room].rstrip('，, ')}，{extra}"
    return validate_prompt(text, limit)


def _cooldown_key(ctx: Context) -> tuple[int, int, int]:
    return (int(ctx.self_id or 0), int(ctx.group_id or 0), int(ctx.user_id or 0))


def _cooldown_left(ctx: Context, seconds: int) -> int:
    if seconds <= 0:
        return 0
    now = time.monotonic()
    if len(_LAST) > 1024:
        for key, value in list(_LAST.items()):
            if value + 3600 < now:
                _LAST.pop(key, None)
    left = _LAST.get(_cooldown_key(ctx), 0.0) + seconds - now
    return max(0, math.ceil(left))


def _failure(result: dict[str, Any]) -> str:
    return str(result.get("message") or "图片生成失败，请稍后再试。")


async def _draw(
    ctx: Context,
    prompt: str,
    size: str = "",
    negative_prompt: str = "",
    *,
    expression: str = "",
    palette: str = "",
    caption: str = "",
    seed: int | None = None,
) -> dict[str, Any]:
    """生成并发送一张图；返回工具结果或带 ``error`` 的可读失败。"""
    settings = ctx.settings
    if not bool(settings.get("enabled", True)):
        return {"error": "imagegen_disabled", "message": "图片生成在本群没有启用。"}
    decision = getattr(ctx, "trigger_decision", None)
    if decision is not None and not getattr(decision, "addressed", True):
        return {
            "error": "imagegen_addressed_required",
            "message": "自动参与时不会生成图片，需要群友 @ 或唤醒词明确请求。",
        }
    try:
        provider = str(settings.get("provider") or "local").strip().casefold()
        if provider not in PROVIDER_OPTIONS:
            provider = "local"
        if provider == "local":
            guessed_expression, guessed_palette = local.analyze_prompt(prompt)
            expression = expression if expression in local.EXPRESSIONS else guessed_expression
            palette = palette if palette in local.PALETTES else guessed_palette
            caption_text = " ".join(str(caption or "").split())[:16]
            width, height = parse_size(size or settings.get("size"), "1024x1024")
            pixels = min(1024, max(320, max(width, height)))
            text = " ".join(str(prompt or "").split())
            if not text:
                raise ImageGenError("imagegen_prompt_missing", "请描述想画的内容。")
        else:
            engine = _engine(settings)
            text = _compose_prompt(
                prompt,
                settings.get("style"),
                int(settings.get("max_prompt_chars") or 700),
            )
            negative = ", ".join(
                item
                for item in (
                    " ".join(str(settings.get("negative_prompt") or "").split()),
                    " ".join(str(negative_prompt or "").split()),
                )
                if item
            )
    except ImageGenError as exc:
        return {"error": exc.code, "message": exc.message}
    wait = _cooldown_left(ctx, int(settings.get("cooldown_seconds") or 0))
    if wait:
        return {
            "error": "imagegen_cooldown",
            "retry_after": wait,
            "message": f"刚生成过图片，{wait} 秒后再试。",
        }
    _LAST[_cooldown_key(ctx)] = time.monotonic()
    try:
        if provider == "local":
            image = await asyncio.to_thread(
                local.render,
                expression=expression,
                palette=palette,
                caption=caption_text,
                seed=seed,
                size=pixels,
                font_path=str(settings.get("caption_font") or ""),
            )
        else:
            async with _SLOTS:
                image = await generate(engine, text, size, negative)
    except ImageGenError as exc:
        ctx.logger.warning("图片生成失败：%s", exc.code)
        return {"error": exc.code, "message": exc.message}
    except Exception:  # noqa: BLE001 - 生成失败不应打断整轮对话
        ctx.logger.exception("图片生成出现未预期错误")
        return {"error": "imagegen_failed", "message": "图片生成失败，请稍后再试。"}

    encoded = base64.b64encode(image).decode("ascii")
    segments = [MessageSegment.image("base64://" + encoded)]
    content = f"[图片] {text}"[:300]
    sent = await send_media_reply(ctx, segments, content, require_ai=bool(ctx.tool_execution_id))
    if not sent:
        return {"sent": False, "error": "imagegen_not_delivered", "message": "图片没有送达，会话可能已经结束。"}
    if provider == "local":
        ctx.logger.info("本地图片生成完成：%s/%s %s 字节", expression, palette, len(image))
        return {
            "sent": True,
            "provider": "local",
            "expression": expression,
            "palette": palette,
            "size": f"{pixels}x{pixels}",
            "bytes": len(image),
            "note": "已发送本喵的猫娘形象；只简短回应一句。本地模式画不了别的角色或写实场景。",
        }
    ctx.logger.info("图片生成完成：%s %s %s 字节", engine.provider, size or engine.size, len(image))
    return {
        "sent": True,
        "provider": engine.provider,
        "size": size or engine.size,
        "bytes": len(image),
        "note": "图片已发送；只简短回应一句，不要重复描述画面或提示词。",
    }


@plugin.on_command("画", aliases=("画图", "绘图", "draw"), cooldown=3.0)
async def draw_command(ctx: Context) -> None:
    """/画 <提示词> —— 生成一张图片"""
    prompt = (ctx.args or "").strip()
    if not prompt:
        await ctx.reply(
            "用法：/画 <想画的内容>\n例如：/画 生气地鼓着腮帮子\n　　　/画 雨夜，委屈巴巴\n　　　/画 猫猫 开心",
            quote=True,
        )
        return
    result = await _draw(ctx, prompt)
    if not result.get("sent"):
        await ctx.reply(_failure(result), quote=True)


@plugin.on_ai_tool(
    "draw",
    title="生成图片",
    effect="side_effecting",
    capabilities=("qq.send",),
    timeout=300,
    max_output_chars=1200,
    description=(
        "画一张图并发送到当前会话。默认 local 模式用 Pillow 现画机器人自己的猫娘形象"
        "（银灰发、猫耳、黑旗袍），适合表情包、头像和情绪图：prompt 写一句画面或情绪，"
        "还可以指定 expression（normal/happy/angry/sad/shy/sleepy/surprised/smug/love/cry/blank）、"
        "palette（night/sakura/rain/sunset/mint/mono）、最 16 字的 caption 和 seed。"
        "省略 expression/palette 时按 prompt 关键词判断。local 画不了别的角色、写实照片或复杂构图，"
        "遇到这类请求要如实说明，不要用文字假装画好。"
        "管理员配置了远程图片模型时，同一工具会改走远程服务，可以画任意画面。"
        "生成成功后只简短回应，不要重复描述画面或提示词；同一请求不要连续生成多张。"
        "还没有配置远程服务、被沙盒关闭或服务报错时如实说明。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": 2000,
                "description": "一句中文画面或情绪描述，例如「生气地鼓着腮帮子」「雨夜，委屈巴巴」",
            },
            "expression": {
                "type": "string",
                "enum": list(local.EXPRESSIONS),
                "description": "可选：表情，省略时按 prompt 关键词判断",
            },
            "palette": {
                "type": "string",
                "enum": list(local.PALETTES),
                "description": "可选：配色场景，省略时按 prompt 关键词判断",
            },
            "caption": {
                "type": "string",
                "maxLength": 16,
                "description": "可选：图片上最多 16 字的短文字，例如「本喵生气啦」；省略则不加文字",
            },
            "seed": {
                "type": "integer",
                "minimum": 0,
                "maximum": 2147483647,
                "description": "可选：随机种子；同一 seed 出图稳定，换 seed 可换一张",
            },
            "size": {
                "type": "string",
                "maxLength": 16,
                "description": "可选：图片尺寸，格式为「宽x高」，例如 768x768；本地模式按最长边生成正方形",
            },
            "negative_prompt": {
                "type": "string",
                "maxLength": 300,
                "description": "可选：仅远程模式使用，这次不想出现的内容",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
)
async def draw(
    ctx: Context,
    prompt: str,
    expression: str = "",
    palette: str = "",
    caption: str = "",
    seed: int | None = None,
    size: str = "",
    negative_prompt: str = "",
) -> dict[str, Any]:
    return await _draw(
        ctx,
        prompt,
        size,
        negative_prompt,
        expression=expression,
        palette=palette,
        caption=caption,
        seed=seed,
    )


__all__ = ["draw", "draw_command", "plugin"]
