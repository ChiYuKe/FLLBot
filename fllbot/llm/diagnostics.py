"""模型功能探测：只调用模型，不执行任何插件工具或向 QQ 发送消息。"""

from __future__ import annotations

import base64
import json
import struct
import time
import zlib

from .client import LLMError


def probe_image() -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    # A solid red 64 x 64 image, without an external URL or user data.
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\x00\x00" * 64) * 64)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


async def diagnose(chat, provider: str | None = None) -> dict:
    if provider and provider not in chat.config.provider_names():
        raise LLMError("模型服务不存在")
    name, config = chat.client.resolve(provider=provider)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "diagnostic_echo",
                "description": "诊断工具：仅返回给定文本，不执行外部操作。",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string", "enum": ["FLLBOT_OK"]}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    probes = [
        ("chat", True, [{"role": "user", "content": "Reply with exactly FLLBOT_OK."}], None),
        (
            "tools",
            config.supports_tools,
            [{"role": "user", "content": 'Call diagnostic_echo once with text="FLLBOT_OK". Do not answer in text.'}],
            tools,
        ),
        (
            "images",
            config.supports_images,
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "What is the dominant color of this image? Reply with one English color word.",
                        },
                        {"type": "image_url", "image_url": {"url": probe_image()}},
                    ],
                }
            ],
            None,
        ),
    ]
    checks = []
    for kind, enabled, messages, definitions in probes:
        if not enabled:
            checks.append({"kind": kind, "status": "skipped", "detail": "模型能力设置已关闭此功能", "latency_ms": 0})
            continue
        started = time.perf_counter()
        try:
            reply = await chat.complete(
                messages,
                provider=name,
                scene="diagnostic",
                task="diagnostic",
                tools=definitions,
                max_tokens=128,
                allow_fallback=False,
            )
            if kind == "tools":
                valid = len(reply.tool_calls) == 1
                function = reply.tool_calls[0].get("function", {}) if valid else {}
                arguments = function.get("arguments", "{}")
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                valid = valid and function.get("name") == "diagnostic_echo" and arguments == {"text": "FLLBOT_OK"}
            else:
                valid = (
                    reply.text.strip() == "FLLBOT_OK"
                    if kind == "chat"
                    else reply.text.strip().lower().strip(".!。 ") == "red"
                )
            valid = valid and reply.finish_reason != "length"
            checks.append(
                {
                    "kind": kind,
                    "status": "passed" if valid else "failed",
                    "detail": "功能验证通过" if valid else "服务返回成功，但响应未满足本次功能探测要求",
                }
            )
        except (LLMError, ValueError, TypeError) as exc:
            # Upstream errors may echo request credentials; keep diagnostics safe for display.
            detail = str(exc)
            key = chat.client._api_key(config)
            if key:
                detail = detail.replace(key, "[密钥已隐藏]")
            checks.append({"kind": kind, "status": "failed", "detail": detail[:500]})
        checks[-1]["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return {
        "provider": name,
        "model": config.model,
        "checks": checks,
        "ok": all(item["status"] != "failed" for item in checks),
    }
