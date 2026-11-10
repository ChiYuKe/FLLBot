"""Bounded image snapshots for the history viewer, separate from model text."""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
from io import BytesIO
from urllib.parse import urlsplit

import httpx
from PIL import Image

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGES = 4


async def public_image_source(source: str) -> bool:
    """Image message URLs must not become a way to query local services."""
    try:
        url = urlsplit(source)
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
            return False
        addresses = await asyncio.to_thread(socket.getaddrinfo, url.hostname, url.port or 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(address[4][0]).is_global for address in addresses)
    except (OSError, ValueError):
        return False


def image_data_url(raw: bytes) -> str:
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        return ""
    try:
        with Image.open(BytesIO(raw)) as image:
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}.get(image.format)
            if not mime or image.width * image.height > 40_000_000:
                return ""
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError):
        return ""
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")


async def snapshot_images(segments) -> list[str]:
    sources = []
    for segment in segments:
        if segment.type != "image":
            continue
        source = str(segment.data.get("url") or segment.data.get("file") or "")
        if source and source not in sources:
            sources.append(source)
        if len(sources) >= MAX_IMAGES:
            break
    if not sources:
        return []
    result = []
    try:
        async with asyncio.timeout(8), httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False) as client:
            for source in sources:
                try:
                    if source.startswith(("base64://", "data:image/")):
                        encoded = source[9:] if source.startswith("base64://") else source.split(";base64,", 1)[1]
                        if len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                            continue
                        raw = base64.b64decode(encoded, validate=True)
                    elif source.startswith(("http://", "https://")):
                        if not await public_image_source(source):
                            continue
                        raw = bytearray()
                        async with client.stream("GET", source) as response:
                            response.raise_for_status()
                            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                                raw.extend(chunk)
                                if len(raw) > MAX_IMAGE_BYTES:
                                    break
                    else:
                        continue
                    url = image_data_url(bytes(raw))
                    if url and url not in result:
                        result.append(url)
                except (httpx.HTTPError, ValueError, IndexError, binascii.Error):
                    continue
    except TimeoutError:
        pass
    return result


async def input_images(ctx) -> list[str]:
    if getattr(ctx, 'history_images', None) is None:
        ctx.history_images = await snapshot_images(ctx.message)
    return ctx.history_images
