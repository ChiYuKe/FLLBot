"""屏幕截图：只负责抓取与编码，不发送消息、不写文件。"""

from __future__ import annotations

import io

from PIL import Image, ImageGrab

MAX_BYTES = 8_000_000
FORMATS = {"png": "PNG", "jpeg": "JPEG"}


class CaptureError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _encode(image: Image.Image, image_format: str, quality: int) -> tuple[bytes, str]:
    fmt = FORMATS.get(image_format.casefold(), "PNG")
    work = image
    if fmt == "JPEG" and work.mode not in {"RGB", "L"}:
        work = work.convert("RGB")
    buffer = io.BytesIO()
    if fmt == "JPEG":
        work.save(buffer, format=fmt, quality=quality, optimize=True)
    else:
        work.save(buffer, format=fmt, optimize=True)
    return buffer.getvalue(), fmt.casefold()


def capture(*, all_screens: bool = False, max_width: int = 1600,
            image_format: str = "png", quality: int = 85) -> dict:
    """抓取屏幕并返回编码后的图片；失败时抛出可安全展示的 CaptureError。"""
    try:
        image = ImageGrab.grab(all_screens=bool(all_screens))
    except ImportError:
        raise CaptureError("screenshot_unavailable", "当前环境缺少屏幕截图依赖") from None
    except (OSError, RuntimeError, TypeError):
        raise CaptureError("screenshot_failed", "无法截取当前屏幕：当前会话可能没有可见桌面") from None
    width = max(320, min(3840, int(max_width)))
    if image.width > width:
        height = max(1, round(image.height * width / image.width))
        image = image.resize((width, height), Image.LANCZOS)
    data, used = _encode(image, image_format, max(60, min(95, int(quality))))
    if len(data) > MAX_BYTES:
        # PNG 截屏可能很大；先退回质量 82 的 JPEG，仍超限时明确失败。
        data, used = _encode(image, "jpeg", 82)
    if len(data) > MAX_BYTES:
        raise CaptureError("screenshot_too_large", "截图超过 8 MB，请降低插件中的最大宽度")
    return {
        "data": data,
        "format": used,
        "width": image.width,
        "height": image.height,
        "bytes": len(data),
    }


__all__ = ["CaptureError", "MAX_BYTES", "capture"]
