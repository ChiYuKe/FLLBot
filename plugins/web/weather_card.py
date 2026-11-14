# Plugin: web | Role: weather card renderer | Metadata: plugin.toml
"""把天气查询结果画成一张 PNG 天气卡片。

使用 Pillow 排版，天气图标来自随插件打包的 Meteocons Fill PNG 素材。
"""

from __future__ import annotations

import io
import math
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

_WIDTH = 900
_SUPERSAMPLE = 2

# 主题：顶部色、底部色；按天气代码和昼夜选择。
_THEMES = {
    "clear_day": ((30, 74, 128), (10, 15, 22)),
    "clear_night": ((12, 22, 44), (8, 10, 14)),
    "partly_day": ((28, 66, 112), (10, 15, 22)),
    "partly_night": ((14, 26, 48), (8, 10, 14)),
    "cloud": ((30, 46, 64), (10, 13, 18)),
    "rain": ((24, 46, 70), (9, 12, 17)),
    "snow": ((38, 56, 78), (10, 13, 18)),
    "thunder": ((24, 24, 40), (8, 9, 13)),
    "fog": ((48, 54, 62), (12, 14, 17)),
}

_DIM = (207, 223, 239)
_FAINT = (157, 180, 204)
_INK = (242, 248, 255)
_AMBER = (255, 192, 117)
_GREEN = (128, 225, 182)

_REGULAR_FONTS = (
    "msyh.ttc",
    "simhei.ttf",
    "simsun.ttc",
    "SourceHanSansSC-Regular.otf",
    "NotoSansCJK-Regular.ttc",
    "NotoSansSC-Regular.otf",
    "PingFang.ttc",
    "wqy-zenhei.ttc",
)
_BOLD_FONTS = (
    "msyhbd.ttc",
    "SourceHanSansSC-Bold.otf",
    "NotoSansCJK-Bold.ttc",
    "NotoSansSC-Bold.otf",
    "simhei.ttf",
    "PingFang.ttc",
    "wqy-zenhei.ttc",
)
_FONT_DIRS = (
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
    Path("/usr/share/fonts/opentype/noto"),
    Path("/usr/share/fonts/truetype/noto"),
    Path("/usr/share/fonts/truetype/wqy"),
    Path("/usr/share/fonts"),
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
)


@lru_cache(maxsize=64)
def _font(font_path: str, size: int, bold: bool) -> ImageFont.FreeTypeFont:
    """按配置或系统字体目录查找中文字体；找不到时抛出 ValueError。"""
    candidates: list[str] = []
    if font_path:
        candidates.append(font_path)
    else:
        names = _BOLD_FONTS if bold else _REGULAR_FONTS
        for directory in _FONT_DIRS:
            candidates.extend(str(directory / name) for name in names)
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    raise ValueError("找不到中文字体，请在 web 插件配置里填写可用的字体文件路径")


def _theme_for(kind: str, is_day: bool) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    if kind == "sun":
        return _THEMES["clear_day"]
    if kind == "moon":
        return _THEMES["clear_night"]
    if kind in {"partly", "partly_night"}:
        return _THEMES["partly_day" if is_day and kind == "partly" else "partly_night"]
    return {
        "cloud": _THEMES["cloud"],
        "overcast": _THEMES["cloud"],
        "fog": _THEMES["fog"],
        "drizzle": _THEMES["rain"],
        "rain": _THEMES["rain"],
        "snow": _THEMES["snow"],
        "thunder": _THEMES["thunder"],
    }.get(kind, _THEMES["cloud"])


def _icon_kind(code: Any, is_day: bool = True) -> str:
    try:
        value = int(code)
    except (TypeError, ValueError):
        return "cloud"
    if value == 0:
        return "sun" if is_day else "moon"
    if value == 1:
        return "sun" if is_day else "moon"
    if value == 2:
        return "partly" if is_day else "partly_night"
    if value == 3:
        return "overcast"
    if value in {45, 48}:
        return "fog"
    if value in {51, 53, 55, 56, 57}:
        return "drizzle"
    if value in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "rain"
    if value in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if value in {95, 96, 99}:
        return "thunder"
    return "cloud"


class _Card:
    """逻辑坐标绘制：内部按 _SUPERSAMPLE 放大，最后缩回逻辑尺寸得到平滑边缘。"""

    def __init__(self, width: int, height: int) -> None:
        self.scale = _SUPERSAMPLE
        self.width = width
        self.height = height
        self.image = Image.new("RGB", (width * self.scale, height * self.scale))
        self.draw = ImageDraw.Draw(self.image)
        self.layer = Image.new("RGBA", self.image.size, (0, 0, 0, 0))
        self.inks = ImageDraw.Draw(self.layer)
        self._fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
        self.font_path = ""

    # -- 基础绘制 --------------------------------------------------------- #
    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def box(self, x0: float, y0: float, x1: float, y1: float) -> tuple[int, int, int, int]:
        return self.px(x0), self.px(y0), self.px(x1), self.px(y1)

    def font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        key = (size, bold)
        if key not in self._fonts:
            self._fonts[key] = _font(self.font_path, self.px(size), bold)
        return self._fonts[key]

    def text(
        self,
        xy: tuple[float, float],
        text: str,
        size: int,
        fill: tuple[int, int, int, int] = _INK,
        *,
        anchor: str = "la",
        bold: bool = False,
    ) -> None:
        self.draw.text(
            (self.px(xy[0]), self.px(xy[1])),
            text,
            font=self.font(size, bold),
            fill=tuple(fill[:3]),
            anchor=anchor,
        )

    def length(self, text: str, size: int, bold: bool = False) -> float:
        return self.draw.textlength(text, font=self.font(size, bold)) / self.scale

    def fit(self, text: str, size: int, limit: float, *, bold: bool = False, minimum: int = 16) -> int:
        current = size
        while current > minimum and self.length(text, current, bold) > limit:
            current -= 2
        return current

    def rounded(
        self,
        box: tuple[float, float, float, float],
        radius: float,
        fill: tuple[int, int, int, int],
        *,
        alpha: int = 255,
    ) -> None:
        self.inks.rounded_rectangle(
            self.box(*box), radius=self.px(radius), fill=(fill[0], fill[1], fill[2], alpha)
        )

    def ellipse(
        self, box: tuple[float, float, float, float], fill: tuple[int, int, int, int], *, alpha: int = 255
    ) -> None:
        self.inks.ellipse(self.box(*box), fill=(fill[0], fill[1], fill[2], alpha))

    def capsule(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        width: float,
        fill: tuple[int, int, int, int],
        *,
        alpha: int = 255,
    ) -> None:
        color = (fill[0], fill[1], fill[2], alpha)
        self.inks.line((self.px(start[0]), self.px(start[1]), self.px(end[0]), self.px(end[1])), fill=color,
                       width=self.px(width))
        radius = width / 2
        for x, y in (start, end):
            self.inks.ellipse(
                self.box(x - radius, y - radius, x + radius, y + radius), fill=color
            )

    def polygon(self, points: list[tuple[float, float]], fill: tuple[int, int, int, int], *, alpha: int = 255) -> None:
        self.inks.polygon([(self.px(x), self.px(y)) for x, y in points], fill=(fill[0], fill[1], fill[2], alpha))

    # -- 收尾 ------------------------------------------------------------- #
    def commit(self) -> None:
        """把已绘制的半透明形状合入底图，之后画的文字才不会被面板盖住。"""
        self.image = Image.alpha_composite(self.image.convert("RGBA"), self.layer).convert("RGB")
        self.draw = ImageDraw.Draw(self.image)
        self.layer = Image.new("RGBA", self.image.size, (0, 0, 0, 0))
        self.inks = ImageDraw.Draw(self.layer)

    def save(self) -> bytes:
        self.commit()
        if self.scale != 1:
            target = (self.width, self.height)
            self.image = self.image.resize(target, Image.LANCZOS)
        output = io.BytesIO()
        self.image.save(output, format="PNG", optimize=True)
        return output.getvalue()


def _gradient(card: _Card, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> None:
    height = card.height * card.scale
    strip = Image.new("RGB", (1, height))
    pixels = strip.load()
    for y in range(height):
        ratio = y / max(1, height - 1)
        pixels[0, y] = tuple(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
    card.image.paste(strip.resize(card.image.size, Image.BILINEAR), (0, 0))
    card.draw = ImageDraw.Draw(card.image)


# --------------------------------------------------------------------------- #
# 图标
# --------------------------------------------------------------------------- #


_WEATHER_ICONS = Path(__file__).with_name("assets") / "meteocons"
_ICON_FILES = {
    "sun": "clear-day",
    "moon": "clear-night",
    "partly": "partly-cloudy-day",
    "partly_night": "partly-cloudy-night",
    "cloud": "cloudy",
    "overcast": "overcast",
    "fog": "fog",
    "drizzle": "drizzle",
    "rain": "rain",
    "snow": "snow",
    "thunder": "thunderstorms",
}


@lru_cache(maxsize=len(_ICON_FILES))
def _icon_image(name: str) -> Image.Image:
    with Image.open(_WEATHER_ICONS / f"{name}.png") as source:
        image = source.convert("RGBA")
    return image.crop(image.getchannel("A").getbbox())


def _icon(card: _Card, kind: str, cx: float, cy: float, size: float) -> None:
    image = _icon_image(_ICON_FILES.get(kind, "cloudy"))
    edge = card.px(size)
    scale = edge / max(image.size)
    width, height = (max(1, round(side * scale)) for side in image.size)
    icon = image.resize((width, height), Image.Resampling.LANCZOS)
    card.layer.alpha_composite(icon, (card.px(cx) - width // 2, card.px(cy) - height // 2))


# --------------------------------------------------------------------------- #
# 卡片排版
# --------------------------------------------------------------------------- #


def _weekday_short(date_text: str) -> str:
    try:
        return "周" + "一二三四五六日"[datetime.strptime(date_text, "%Y-%m-%d").weekday()]
    except (TypeError, ValueError):
        return ""


def _month_day(date_text: str) -> str:
    try:
        moment = datetime.strptime(date_text, "%Y-%m-%d")
        return f"{moment.month}月{moment.day}日"
    except (TypeError, ValueError):
        return date_text or ""


def _day_label(index: int, weekday: str) -> str:
    if index == 0:
        return "今天"
    if index == 1:
        return "明天"
    if index == 2:
        return "后天"
    return weekday or f"第 {index + 1} 天"


def _value(value: Any, digits: int = 1) -> str:
    try:
        return f"{round(float(value), digits):g}"
    except (TypeError, ValueError):
        return "--"


def _icon_for(item: dict[str, Any], is_day: bool = True) -> str:
    """数据源可以直接给 icon；没有时按 Open-Meteo 的天气代码推断。"""
    kind = str(item.get("icon") or "").strip()
    return kind or _icon_kind(item.get("code"), is_day)


def _row_tail(item: dict[str, Any]) -> str:
    rain = item.get("precip_prob_percent")
    if rain is not None:
        return f"降水 {_value(rain, 0)}%"
    if item.get("aqi_name"):
        return f"空气 {item['aqi_name']}"
    if item.get("aqi") is not None:
        return f"AQI {item['aqi']}"
    return ""


def _polyline(
    card: _Card,
    points: list[tuple[float, float]],
    width: float,
    color: tuple[int, int, int],
    alpha: int = 230,
) -> None:
    for start, end in zip(points, points[1:], strict=False):
        card.capsule(start, end, width, color, alpha=alpha)


def _status_color(info: str) -> tuple[int, int, int]:
    text = str(info or "")
    if "不易发" in text or "最弱" in text:
        return _GREEN
    if any(word in text for word in ("不宜", "较易发", "易发", "很强", "强", "高", "严重", "较差")):
        return _AMBER
    if any(word in text for word in ("适宜", "良好", "优", "不易发", "弱", "低", "舒适")):
        return _GREEN
    return _DIM


def _index_icon(card: _Card, key: str, cx: float, cy: float, size: float) -> None:
    s = size
    color = (151, 185, 216)
    if key == "drying":
        _icon(card, "sun", cx - s * 0.24, cy - s * 0.2, s * 0.55)
        card.capsule((cx - s * 0.45, cy + s * 0.04), (cx + s * 0.45, cy + s * 0.04), s * 0.05, color, alpha=220)
        for offset in (-0.18, 0.2):
            x = cx + s * offset
            card.polygon(
                [
                    (x - s * 0.1, cy + s * 0.08),
                    (x + s * 0.1, cy + s * 0.08),
                    (x + s * 0.13, cy + s * 0.36),
                    (x - s * 0.13, cy + s * 0.36),
                ],
                color,
                alpha=220,
            )
    elif key == "sports":
        card.inks.ellipse(
            card.box(cx - s * 0.36, cy - s * 0.36, cx + s * 0.36, cy + s * 0.36),
            outline=(*color, 220),
            width=card.px(2),
        )
        card.capsule((cx - s * 0.36, cy), (cx + s * 0.36, cy), s * 0.05, color, alpha=200)
        card.capsule((cx, cy - s * 0.36), (cx, cy + s * 0.36), s * 0.05, color, alpha=160)
    elif key == "morning":
        _icon(card, "sun", cx, cy - s * 0.08, s * 0.7)
        card.capsule((cx - s * 0.46, cy + s * 0.3), (cx + s * 0.46, cy + s * 0.3), s * 0.06, color, alpha=220)
    elif key == "ultraviolet":
        _icon(card, "sun", cx, cy, s * 0.9)
        card.inks.ellipse(
            card.box(cx - s * 0.48, cy - s * 0.48, cx + s * 0.48, cy + s * 0.48),
            outline=(*color, 150),
            width=card.px(1.5),
        )
    elif key == "allergy":
        for index in range(5):
            angle = math.tau * index / 5
            petal_x = cx + math.cos(angle) * s * 0.22
            petal_y = cy + math.sin(angle) * s * 0.22
            card.ellipse(
                (petal_x - s * 0.13, petal_y - s * 0.13, petal_x + s * 0.13, petal_y + s * 0.13),
                color,
                alpha=210,
            )
        card.ellipse((cx - s * 0.09, cy - s * 0.09, cx + s * 0.09, cy + s * 0.09), (255, 214, 120), alpha=230)
    elif key == "clothes":
        points = [
            (cx - s * 0.16, cy - s * 0.3),
            (cx - s * 0.44, cy - s * 0.1),
            (cx - s * 0.28, cy + s * 0.04),
            (cx - s * 0.24, cy + s * 0.38),
            (cx + s * 0.24, cy + s * 0.38),
            (cx + s * 0.28, cy + s * 0.04),
            (cx + s * 0.44, cy - s * 0.1),
            (cx + s * 0.16, cy - s * 0.3),
            (cx, cy - s * 0.2),
        ]
        _polyline(card, [*points, points[0]], s * 0.055, color)
    elif key == "makeup":
        card.rounded((cx - s * 0.1, cy + s * 0.02, cx + s * 0.14, cy + s * 0.38), s * 0.05, color, alpha=220)
        card.polygon(
            [
                (cx - s * 0.1, cy + s * 0.02),
                (cx + s * 0.14, cy + s * 0.02),
                (cx + s * 0.04, cy - s * 0.28),
                (cx - s * 0.06, cy - s * 0.24),
            ],
            color,
            alpha=220,
        )
    elif key == "cold":
        card.rounded((cx - s * 0.09, cy - s * 0.4, cx + s * 0.09, cy + s * 0.16), s * 0.09, color, alpha=220)
        card.ellipse((cx - s * 0.2, cy + s * 0.06, cx + s * 0.2, cy + s * 0.4), color, alpha=220)
        card.capsule((cx - s * 0.15, cy - s * 0.12), (cx - s * 0.02, cy - s * 0.12), s * 0.04, (16, 20, 26), alpha=230)
        card.capsule((cx - s * 0.15, cy + s * 0.04), (cx - s * 0.02, cy + s * 0.04), s * 0.04, (16, 20, 26), alpha=230)
    else:
        card.inks.ellipse(
            card.box(cx - s * 0.3, cy - s * 0.3, cx + s * 0.3, cy + s * 0.3),
            outline=(*color, 200),
            width=card.px(2),
        )


def _wrap(card: _Card, text: str, size: int, width: float, max_lines: int = 2) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    lines: list[str] = []
    current = ""
    truncated = False
    for char in text:
        # 行首标点不单独换行，宁可这一行略微超宽。
        if current and char not in "，。！？、；：）》」』”’" and card.length(current + char, size) > width:
            lines.append(current)
            current = char
            if len(lines) >= max_lines:
                truncated = True
                break
        else:
            current += char
    if not truncated and current and len(lines) < max_lines:
        lines.append(current)
    if truncated and lines:
        lines[-1] = lines[-1][:-1] + "…"
    return lines[:max_lines]



_ACCENT = (101, 192, 255)
_RULE = (48, 68, 91)
_TILE = (44, 65, 88)
_SURFACE = (24, 39, 57)
_WHITE = (255, 255, 255)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _hero(card: _Card, data: dict[str, Any], current: dict[str, Any], today: dict[str, Any]) -> None:
    is_day = bool(current.get("is_day", True))
    kind = _icon_for(current, is_day)
    top, bottom = _theme_for(kind, is_day)
    for y in range(266):
        ratio = y / 265
        color = tuple(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        card.draw.line(card.box(0, y, _WIDTH, y), fill=color, width=card.scale)

    card.ellipse((586, -115, 1020, 319), _WHITE, alpha=12)
    card.ellipse((627, -74, 979, 278), _WHITE, alpha=13)
    card.ellipse((672, -29, 934, 233), _WHITE, alpha=15)
    card.commit()
    _icon(card, kind, 769, 110, 146)
    card.commit()

    city = str(data.get("city") or "当前城市")
    region = str(data.get("region") or "")
    card.text((48, 27), city, card.fit(city, 34, 550, bold=True), _WHITE, bold=True)
    if region and region != city:
        card.text((50, 74), region, 16, (188, 210, 231))
    temp = _value(current.get("temperature_c"))
    card.text((42, 94), temp, 92, _WHITE, bold=True)
    card.text((54 + card.length(temp, 92, True), 116), "°C", 30, (186, 217, 243))

    condition = str(current.get("weather") or today.get("weather") or "")
    low, high = today.get("temp_min_c"), today.get("temp_max_c")
    if low is not None and high is not None:
        condition = "   ·   ".join(filter(None, (condition, f"{_value(low)}° / {_value(high)}°")))
    card.text((50, 209), condition, 21, _WHITE)

    air = data.get("air") or {}
    wind = current.get("wind_text")
    if not wind and current.get("wind_kmh") is not None:
        wind = f"{_value(current['wind_kmh'])} km/h"
    humidity = current.get("humidity_percent")
    metrics = [
        ("风况", wind),
        ("湿度", f"{_value(humidity, 0)}%" if humidity is not None else ""),
        ("空气质量", air.get("name") or (f"AQI {air['aqi']}" if air.get("aqi") is not None else "")),
    ]
    metrics = [(label, str(value)) for label, value in metrics if value]
    if not metrics:
        return
    card.rounded((28, 250, 872, 331), 18, _SURFACE)
    step = 844 / len(metrics)
    for index in range(1, len(metrics)):
        x = 28 + index * step
        card.capsule((x, 271), (x, 311), 1, _RULE)
    card.commit()
    for index, (label, value) in enumerate(metrics):
        x = 28 + (index + 0.5) * step
        card.text((x, 265), label, 15, _FAINT, anchor="ma")
        card.text((x, 286), value, card.fit(value, 23, step - 32, bold=True), _INK, anchor="ma", bold=True)


def _hourly(card: _Card, items: list[dict[str, Any]], y: int) -> None:
    card.rounded((28, y, 872, y + 190), 18, _SURFACE)
    card.commit()
    values = [_number(item.get("degree_c")) for item in items]
    known = [value for value in values if value is not None]
    low, high = (min(known), max(known)) if known else (0, 0)
    step = 804 / len(items)
    previous = None
    slots = []
    for index, (item, value) in enumerate(zip(items, values, strict=True)):
        x = 48 + step * (index + 0.5)
        point_y = y + 124 if high == low or value is None else y + 138 - 24 * (value - low) / (high - low)
        if value is not None:
            if previous is not None:
                card.capsule(previous, (x, point_y), 2, (153, 198, 231))
            previous = (x, point_y)
        else:
            previous = None
        slots.append((x, point_y, item, value))
        _icon(card, _icon_for(item), x, y + 66, 31)
    for x, point_y, _, value in slots:
        if value is not None:
            card.ellipse((x - 4, point_y - 4, x + 4, point_y + 4), _ACCENT)
    card.commit()
    card.text((49, y + 17), "逐小时预报", 22, bold=True)
    for x, point_y, item, value in slots:
        card.text((x, point_y - 25), f"{_value(value)}°", 17, _INK, anchor="ma", bold=True)
        card.text((x, y + 151), str(item.get("time") or ""), 16, _FAINT, anchor="ma")


def _daily(card: _Card, items: list[dict[str, Any]], y: int) -> None:
    card.rounded((28, y, 872, y + 62 + len(items) * 57), 18, _SURFACE)
    card.commit()
    ranges = [(_number(item.get("temp_min_c")), _number(item.get("temp_max_c"))) for item in items]
    values = [value for pair in ranges for value in pair if value is not None]
    low, high = (min(values), max(values)) if values else (0, 0)
    for index, (item, (start, end)) in enumerate(zip(items, ranges, strict=True)):
        row = y + 52 + index * 57
        if index:
            card.capsule((48, row), (852, row), 1, _RULE)
        _icon(card, _icon_for(item), 174, row + 28, 34)
        card.capsule((438, row + 28), (652, row + 28), 6, _RULE)
        if start is not None and end is not None:
            left = 438 + 214 * (start - low) / (high - low) if high > low else 545
            right = 438 + 214 * (end - low) / (high - low) if high > low else 545
            card.capsule((left, row + 28), (right, row + 28), 6, _ACCENT)
    card.commit()
    card.text((49, y + 17), f"未来 {len(items)} 天", 22, bold=True)
    for index, (item, (start, end)) in enumerate(zip(items, ranges, strict=True)):
        row = y + 52 + index * 57
        date = item.get("date", "")
        label = _day_label(index, item.get("weekday") or _weekday_short(date))
        card.text((49, row + 6), label, 17, bold=True)
        card.text((49, row + 30), _month_day(date), 13, _FAINT)
        weather = str(item.get("weather") or "--")
        card.text((207, row + 28), weather, card.fit(weather, 17, 145), _DIM, anchor="lm")
        card.text((420, row + 28), f"{_value(start)}°", 17, _FAINT, anchor="rm")
        card.text((670, row + 28), f"{_value(end)}°", 17, _INK, anchor="lm", bold=True)
        card.text((850, row + 28), _row_tail(item), 15, _FAINT, anchor="rm")


def _indices(card: _Card, items: list[dict[str, Any]], y: int) -> None:
    card.text((49, y + 3), "生活指数", 22, bold=True)
    for index, item in enumerate(items):
        x = 28 + index % 2 * 429
        row = y + 43 + index // 2 * 88
        card.rounded((x, row, x + 415, row + 79), 14, _SURFACE)
        card.rounded((x + 12, row + 15, x + 60, row + 63), 12, _TILE)
        _index_icon(card, str(item.get("key") or ""), x + 36, row + 39, 31)
    card.commit()
    for index, item in enumerate(items):
        x = 28 + index % 2 * 429
        row = y + 43 + index // 2 * 88
        name = str(item.get("name") or "")
        info = str(item.get("info") or "")
        card.text((x + 73, row + 9), name, card.fit(name, 17, 198, bold=True), bold=True)
        card.text((x + 400, row + 10), info, card.fit(info, 16, 117), _status_color(info), anchor="ra")
        for line_number, line in enumerate(_wrap(card, item.get("detail") or "", 14, 320)):
            card.text((x + 73, row + 34 + line_number * 18), line, 14, _FAINT)


def render(data: dict[str, Any], *, font_path: str = "") -> bytes:
    """把天气数据渲染为 PNG，按实际数据展示可用区块。"""
    current = data.get("current") or {}
    hourly = list(data.get("hourly") or [])[:7]
    days = list(data.get("daily") or [])
    indices = list(data.get("index") or [])[:8]
    has_metrics = (
        current.get("wind_text") or current.get("wind_kmh") is not None
        or current.get("humidity_percent") is not None or (data.get("air") or {}).get("name")
        or (data.get("air") or {}).get("aqi") is not None
    )
    y = 349 if has_metrics else 284
    height = y + 39
    if hourly:
        height += 208
    if days:
        height += 80 + len(days) * 57
    if indices:
        height += 43 + ((len(indices) + 1) // 2) * 88

    card = _Card(_WIDTH, height)
    card.font_path = font_path
    _gradient(card, (21, 38, 58), (13, 23, 37))
    _hero(card, data, current, days[0] if days else {})
    if hourly:
        _hourly(card, hourly, y)
        y += 208
    if days:
        _daily(card, days, y)
        y += 80 + len(days) * 57
    if indices:
        _indices(card, indices, y)

    footer = [f"数据来源 {data.get('source') or 'Open-Meteo'}"]
    for key, label in (("sunrise", "日出"), ("sunset", "日落")):
        if data.get(key):
            footer.append(f"{label} {data[key]}")
    if len(footer) == 1:
        zone = data.get("utc_offset") or data.get("timezone")
        if zone:
            footer.append(str(zone))
    card.text((450, height - 31), "  ·  ".join(footer), 14, _FAINT, anchor="ma")
    return card.save()
