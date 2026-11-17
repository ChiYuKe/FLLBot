# Plugin: imagegen | Role: provider adapters | Metadata: plugin.toml
"""图片生成服务的协议适配：请求构造、响应解析与图片规范化。

这里不依赖宿主内部模块，便于同一套逻辑被命令、AI 工具和测试复用。
只和配置里的固定服务地址通信；用户提供的提示词只是请求内容，不会被当成链接访问。
"""

from __future__ import annotations

import base64
import binascii
import io
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

PROVIDERS = ("openai", "siliconflow", "sdwebui")
RESPONSE_FORMATS = ("auto", "url", "b64_json")

MIN_IMAGE_EDGE = 64
MAX_IMAGE_EDGE = 4096
MAX_IMAGE_BYTES = 20 * 1024 * 1024
_SUPPORTED_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ImageGenError(Exception):
    """可直接展示给用户的失败；``code`` 供模型和日志区分原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Engine:
    """一次生成所需的不可变配置快照。"""

    provider: str
    base_url: str
    api_key: str
    model: str
    size: str
    negative_prompt: str
    steps: int
    guidance: float
    response_format: str
    timeout: float
    max_bytes: int


def _clean_base_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ImageGenError("imagegen_not_configured", "还没有填写图片服务地址。")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise ImageGenError("imagegen_config_invalid", "图片服务地址格式无效。") from None
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise ImageGenError("imagegen_config_invalid", "图片服务地址必须是 http(s) 链接。")
    if parts.username or parts.password:
        raise ImageGenError("imagegen_config_invalid", "图片服务地址不能包含用户名或密码。")
    return text.rstrip("/")


def validate_engine(
    *,
    provider: Any,
    base_url: Any,
    api_key: Any,
    model: Any,
    size: Any,
    negative_prompt: Any = "",
    steps: Any = 28,
    guidance: Any = 7.5,
    response_format: Any = "auto",
    timeout: Any = 120,
    max_bytes: Any = MAX_IMAGE_BYTES,
) -> Engine:
    """校验管理端配置；缺项直接给出可读提示，不发起请求。"""
    name = str(provider or "").strip().casefold()
    if name not in PROVIDERS:
        raise ImageGenError("imagegen_config_invalid", "未知的图片服务类型。")
    url = _clean_base_url(base_url)
    key = str(api_key or "").strip()
    if name != "sdwebui" and not key:
        raise ImageGenError("imagegen_not_configured", "还没有填写图片服务的 API Key。")
    model_name = str(model or "").strip()
    if name != "sdwebui" and not model_name:
        raise ImageGenError("imagegen_not_configured", "还没有填写图片模型名称。")
    if len(model_name) > 120:
        raise ImageGenError("imagegen_config_invalid", "图片模型名称过长。")
    default_size = parse_size(size, "1024x1024")
    fmt = str(response_format or "auto").strip().casefold()
    if fmt not in RESPONSE_FORMATS:
        fmt = "auto"
    try:
        step_count = max(1, min(100, int(steps or 28)))
        scale = max(1.0, min(30.0, float(guidance or 7.5)))
        wait = max(10.0, min(240.0, float(timeout or 120)))
        byte_limit = max(256 * 1024, min(MAX_IMAGE_BYTES, int(max_bytes or MAX_IMAGE_BYTES)))
    except (TypeError, ValueError):
        raise ImageGenError("imagegen_config_invalid", "图片服务的时间或大小配置无效。") from None
    return Engine(
        provider=name,
        base_url=url,
        api_key=key,
        model=model_name,
        size=f"{default_size[0]}x{default_size[1]}",
        negative_prompt=_clean_text(negative_prompt, 300),
        steps=step_count,
        guidance=scale,
        response_format=fmt,
        timeout=wait,
        max_bytes=byte_limit,
    )


def _clean_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = _CONTROL_RE.sub("", text)
    return text[:limit]


def validate_prompt(value: Any, limit: int = 700) -> str:
    text = " ".join(str(value or "").split())
    text = _CONTROL_RE.sub("", text)
    if not text:
        raise ImageGenError("imagegen_prompt_missing", "请描述想画的内容。")
    if len(text) > limit:
        raise ImageGenError("imagegen_prompt_too_long", f"提示词太长（上限 {limit} 字），请精简画面描述。")
    return text


def parse_size(value: Any, default: str = "1024x1024") -> tuple[int, int]:
    """解析 WxH；尺寸受上下限约束，宽高都按 8 的倍数校验。"""
    text = str(value or "").strip().casefold()
    if not text:
        text = str(default or "").strip().casefold()
    match = re.fullmatch(r"(\d{2,4})\s*[x×*]\s*(\d{2,4})", text)
    if not match:
        raise ImageGenError("imagegen_size_invalid", "图片尺寸请使用「宽x高」，例如 1024x1024。")
    width, height = int(match.group(1)), int(match.group(2))
    if not (MIN_IMAGE_EDGE <= width <= MAX_IMAGE_EDGE and MIN_IMAGE_EDGE <= height <= MAX_IMAGE_EDGE):
        raise ImageGenError("imagegen_size_invalid", f"图片尺寸需在 {MIN_IMAGE_EDGE}–{MAX_IMAGE_EDGE} 像素之间。")
    if width % 8 or height % 8:
        raise ImageGenError("imagegen_size_invalid", "图片宽高都需要是 8 的倍数。")
    return width, height


def _endpoint(engine: Engine) -> str:
    base = engine.base_url
    if engine.provider == "sdwebui":
        return base if base.endswith("/sdapi/v1/txt2img") else base + "/sdapi/v1/txt2img"
    return base if base.endswith("/images/generations") else base + "/images/generations"


def build_payload(engine: Engine, prompt: str, size: str, negative_prompt: str = "") -> dict[str, Any]:
    width, height = parse_size(size, engine.size)
    negative = _clean_text(negative_prompt or engine.negative_prompt, 300)
    if engine.provider == "openai":
        payload: dict[str, Any] = {
            "model": engine.model,
            "prompt": prompt,
            "n": 1,
            "size": f"{width}x{height}",
        }
        if engine.response_format != "auto":
            payload["response_format"] = engine.response_format
        return payload
    if engine.provider == "siliconflow":
        payload = {
            "model": engine.model,
            "prompt": prompt,
            "image_size": f"{width}x{height}",
            "batch_size": 1,
            "num_inference_steps": engine.steps,
            "guidance_scale": engine.guidance,
        }
        if negative:
            payload["negative_prompt"] = negative
        return payload
    payload = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "steps": engine.steps,
        "cfg_scale": engine.guidance,
    }
    if negative:
        payload["negative_prompt"] = negative
    return payload


def _headers(engine: Engine) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if engine.api_key:
        headers["Authorization"] = f"Bearer {engine.api_key}"
    return headers


def _provider_message(response: httpx.Response, secret: str = "") -> str:
    """只提取供应商的可读错误，去掉控制字符并截断，避免把整段响应回给模型。"""
    try:
        data = response.json()
    except ValueError:
        data = None
    detail = ""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif isinstance(error, str):
            detail = error
        if not detail:
            detail = str(data.get("message") or data.get("msg") or data.get("error_description") or "")
    detail = " ".join(_CONTROL_RE.sub("", detail).split())[:160]
    if secret:
        detail = detail.replace(secret, "***")
    return detail


def _candidates(payload: Any) -> list[tuple[str, str]]:
    """把不同供应商的返回统一成 (kind, value)，kind 为 b64 或 url。"""
    if not isinstance(payload, dict):
        return []
    items: list[Any] = []
    for key in ("data", "images"):
        value = payload.get(key)
        if isinstance(value, list):
            items.extend(value)
        elif isinstance(value, dict):
            items.append(value)
        elif isinstance(value, str):
            items.append(value)
    results: list[tuple[str, str]] = []
    for key in ("url", "image_url", "b64_json"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            results.append(("url" if text.startswith(("http://", "https://")) else "b64", text))
    for item in items:
        if isinstance(item, str):
            text = item.strip()
            results.append(("url" if text.startswith(("http://", "https://")) else "b64", text))
        elif isinstance(item, dict):
            encoded = item.get("b64_json") or item.get("b64") or item.get("image")
            if isinstance(encoded, str) and encoded.strip():
                results.append(("b64", encoded))
                continue
            url = item.get("url") or item.get("image_url")
            if isinstance(url, str) and url.strip():
                results.append(("url", url.strip()))
    return results


def _decode_base64(value: str) -> bytes:
    text = value.strip()
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    text = re.sub(r"\s+", "", text)
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ImageGenError("imagegen_invalid_response", "图片服务返回了无法解码的图片数据。") from None


async def _download(client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes:
    """下载供应商给出的图片地址；只接受 http(s)，并有大小上限。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ImageGenError("imagegen_invalid_response", "图片地址格式无效。") from None
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise ImageGenError("imagegen_invalid_response", "图片地址不是 http(s) 链接。")
    try:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise ImageGenError("imagegen_http_error", f"下载生成结果失败（HTTP {response.status_code}）。")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise ImageGenError("imagegen_too_large", "生成的图片超过大小上限。")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise ImageGenError("imagegen_network", f"连接图片服务失败（{type(exc).__name__}）。") from exc


def normalize_image(data: bytes, max_bytes: int) -> bytes:
    """校验图片并统一转成 PNG：顺带去掉了供应商附带的 EXIF 等元数据。"""
    if not data:
        raise ImageGenError("imagegen_invalid_response", "图片服务没有返回图片数据。")
    if len(data) > max_bytes:
        raise ImageGenError("imagegen_too_large", "生成的图片超过大小上限。")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in _SUPPORTED_FORMATS:
                raise ImageGenError("imagegen_invalid_image", "图片服务返回了不支持的图片格式。")
            width, height = probe.size
            if not (MIN_IMAGE_EDGE <= width <= MAX_IMAGE_EDGE and MIN_IMAGE_EDGE <= height <= MAX_IMAGE_EDGE):
                raise ImageGenError("imagegen_invalid_image", "图片服务返回的图片尺寸异常。")
            probe.load()
            has_alpha = probe.mode in {"RGBA", "LA"} or (probe.mode == "P" and "transparency" in probe.info)
            image = probe.convert("RGBA" if has_alpha else "RGB")
    except ImageGenError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise ImageGenError("imagegen_invalid_image", "图片服务返回的数据不是有效图片。") from None
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    result = buffer.getvalue()
    if len(result) > max_bytes:
        raise ImageGenError("imagegen_too_large", "生成的图片超过大小上限。")
    return result


def _new_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(20.0, timeout)),
        follow_redirects=True,
    )


async def generate(
    engine: Engine,
    prompt: str,
    size: str = "",
    negative_prompt: str = "",
    *,
    client_factory: Any = None,
    max_image_bytes: int | None = None,
) -> bytes:
    """请求供应商并返回校验过的 PNG 字节；失败抛出 :class:`ImageGenError`。"""
    factory = client_factory or _new_client
    payload = build_payload(engine, prompt, size, negative_prompt)
    limit = min(engine.max_bytes, max_image_bytes or engine.max_bytes)
    try:
        async with factory(engine.timeout) as client:
            response = await client.post(_endpoint(engine), headers=_headers(engine), json=payload)
            if response.status_code >= 400:
                detail = _provider_message(response, engine.api_key)
                message = f"图片服务返回 HTTP {response.status_code}"
                if detail:
                    message += f"：{detail}"
                raise ImageGenError("imagegen_http_error", message + "。")
            try:
                body = response.json()
            except ValueError:
                raise ImageGenError("imagegen_invalid_response", "图片服务返回的不是 JSON。") from None
            candidates = _candidates(body)
            if not candidates:
                raise ImageGenError("imagegen_empty", "图片服务没有返回图片。")
            last_error: ImageGenError | None = None
            for kind, value in candidates:
                try:
                    raw = _decode_base64(value) if kind == "b64" else await _download(client, value, limit)
                    return normalize_image(raw, limit)
                except ImageGenError as exc:
                    last_error = exc
            raise last_error or ImageGenError("imagegen_invalid_image", "图片服务返回的图片都无法读取。")
    except ImageGenError:
        raise
    except httpx.TimeoutException as exc:
        raise ImageGenError("imagegen_timeout", "图片服务响应超时，请稍后再试或换用更快的模型。") from exc
    except httpx.HTTPError as exc:
        raise ImageGenError("imagegen_network", f"连接图片服务失败（{type(exc).__name__}）。") from exc


__all__ = [
    "Engine",
    "ImageGenError",
    "PROVIDERS",
    "RESPONSE_FORMATS",
    "build_payload",
    "generate",
    "normalize_image",
    "parse_size",
    "validate_engine",
    "validate_prompt",
]
