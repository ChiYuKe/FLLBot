# Plugin: imagegen | Role: local PIL renderer | Metadata: plugin.toml
"""用 Pillow 直接绘制机器人自己的猫娘形象，不依赖任何图片服务。

画的是固定的银灰色猫娘（丸子头、呆毛、猫耳、红旗袍），通过表情、配色和少量前景元素
表达情绪与场景。设计坐标固定为 1000×1000，先按 2–3 倍超采样绘制再缩小，边缘更干净。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from fllbot.plugin import load_font

DESIGN = 1000
EXPRESSIONS = ("normal", "happy", "angry", "sad", "shy", "sleepy", "surprised", "smug", "love", "cry", "blank")
PALETTES = ("night", "sakura", "rain", "sunset", "mint", "mono")

SKIN = (255, 226, 210, 255)
SKIN_SHADE = (242, 196, 178, 255)
HAIR = (214, 219, 228, 255)
HAIR_LIGHT = (238, 241, 246, 255)
HAIR_SHADE = (166, 173, 188, 255)
DRESS = (30, 30, 40, 255)
GOLD = (232, 194, 106, 255)
EYE = (214, 72, 84, 255)
EYE_DARK = (139, 39, 55, 255)
LINE = (58, 44, 50, 255)
BLUSH = (255, 138, 158, 255)
WHITE = (255, 255, 255, 255)

_EXPRESSION_WORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("哭", "呜呜", "大哭", "眼泪", "泪"), "cry"),
    (("爱", "喜欢", "心动", "比心", "爱心"), "love"),
    (("生气", "愤怒", "炸毛", "气死", "讨厌", "哼"), "angry"),
    (("难过", "伤心", "委屈", "失落", "沮丧"), "sad"),
    (("害羞", "脸红", "不好意思", "羞"), "shy"),
    (("困", "睡", "累", "打瞌睡"), "sleepy"),
    (("惊", "吓", "震惊", "意外", "震惊"), "surprised"),
    (("得意", "骄傲", "坏笑", " smug", "嘻"), "smug"),
    (("无语", "呆", "愣", "茫然"), "blank"),
    (("开心", "高兴", "笑", "好耶", "快乐", "可爱"), "happy"),
)
_PALETTE_WORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("雨", "下雨", "阴天", "潮湿"), "rain"),
    (("樱", "花瓣", "春天", "粉色"), "sakura"),
    (("黄昏", "夕阳", "晚霞", "日落"), "sunset"),
    (("薄荷", "绿色", "清新"), "mint"),
    (("黑白", "线稿", "漫画"), "mono"),
    (("夜", "晚上", "月亮", "星", "深夜"), "night"),
)


@dataclass(frozen=True)
class Theme:
    top: tuple[int, int, int]
    bottom: tuple[int, int, int]
    accent: tuple[int, int, int]
    moon: bool
    particle: str


THEMES: dict[str, Theme] = {
    "night": Theme((17, 24, 48), (66, 84, 132), (255, 214, 120), True, "stars"),
    "sakura": Theme((255, 226, 238), (255, 186, 212), (255, 112, 156), False, "petals"),
    "rain": Theme((56, 72, 94), (126, 148, 172), (170, 216, 255), False, "rain"),
    "sunset": Theme((255, 152, 108), (104, 66, 118), (255, 214, 142), True, "stars"),
    "mint": Theme((216, 246, 236), (148, 214, 194), (48, 162, 132), False, "sparkles"),
    "mono": Theme((246, 246, 246), (188, 188, 188), (64, 64, 64), False, "dots"),
}


def analyze_prompt(text: str) -> tuple[str, str]:
    """从自然语言里挑出最接近的表情和配色；识别不到时用开心 + 夜色。"""
    value = str(text or "")
    expression = "happy"
    palette = "night"
    for words, name in _EXPRESSION_WORDS:
        if any(word in value for word in words):
            expression = name
            break
    for words, name in _PALETTE_WORDS:
        if any(word in value for word in words):
            palette = name
            break
    return expression, palette


class Painter:
    """把 0–1000 的设计坐标映射到超采样画布。"""

    def __init__(self, work: int) -> None:
        self.work = work
        self.k = work / DESIGN
        self.img = Image.new("RGBA", (work, work), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)

    # -- 坐标与基础形状 --------------------------------------------------- #
    def p(self, x: float, y: float) -> tuple[float, float]:
        return (x * self.k, y * self.k)

    def points(self, values: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [self.p(x, y) for x, y in values]

    def poly(self, values: list[tuple[float, float]], fill: Any, outline: Any = None, width: float = 0) -> None:
        self.d.polygon(self.points(values), fill=fill, outline=outline, width=max(0, int(width * self.k)))

    def ell(self, cx: float, cy: float, rx: float, ry: float, fill: Any, outline: Any = None, width: float = 0) -> None:
        self.d.ellipse(
            [self.p(cx - rx, cy - ry), self.p(cx + rx, cy + ry)],
            fill=fill,
            outline=outline,
            width=max(0, int(width * self.k)),
        )

    def box(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        fill: Any,
        outline: Any = None,
        width: float = 0,
        radius: float = 0,
    ) -> None:
        rect = [self.p(x0, y0), self.p(x1, y1)]
        if radius:
            self.d.rounded_rectangle(
                rect, radius=radius * self.k, fill=fill, outline=outline, width=max(0, int(width * self.k))
            )
        else:
            self.d.rectangle(rect, fill=fill, outline=outline, width=max(0, int(width * self.k)))

    def line(self, values: list[tuple[float, float]], fill: Any, width: float = 6, joint: str = "curve") -> None:
        self.d.line(self.points(values), fill=fill, width=max(1, int(width * self.k)), joint=joint)

    def arc(
        self, cx: float, cy: float, rx: float, ry: float, start: float, end: float, fill: Any, width: float = 6
    ) -> None:
        self.d.arc(
            [self.p(cx - rx, cy - ry), self.p(cx + rx, cy + ry)],
            start=start,
            end=end,
            fill=fill,
            width=max(1, int(width * self.k)),
        )

    def pie(self, cx: float, cy: float, rx: float, ry: float, start: float, end: float, fill: Any) -> None:
        self.d.pieslice([self.p(cx - rx, cy - ry), self.p(cx + rx, cy + ry)], start=start, end=end, fill=fill)

    def glow(self, draw: Any) -> None:
        """在独立图层上画半透明内容，再与当前画布合成。"""
        layer = Image.new("RGBA", (self.work, self.work), (0, 0, 0, 0))
        draw(ImageDraw.Draw(layer), self)
        self.img = Image.alpha_composite(self.img, layer)
        self.d = ImageDraw.Draw(self.img)

    def glow_blurred(self, draw: Any, radius: float = 30) -> None:
        """模糊发光层；用于月亮和柔和光晕。"""
        layer = Image.new("RGBA", (self.work, self.work), (0, 0, 0, 0))
        draw(ImageDraw.Draw(layer), self)
        layer = layer.filter(ImageFilter.GaussianBlur(max(1, radius * self.k)))
        self.img = Image.alpha_composite(self.img, layer)
        self.d = ImageDraw.Draw(self.img)

    # -- 背景 ------------------------------------------------------------- #
    def gradient(self, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> None:
        base = Image.new("RGB", (self.work, self.work))
        draw = ImageDraw.Draw(base)
        for y in range(self.work):
            t = y / max(1, self.work - 1)
            color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
            draw.line([(0, y), (self.work, y)], fill=color)
        self.img = base.convert("RGBA")
        self.d = ImageDraw.Draw(self.img)


def _bezier(points: list[tuple[float, float]], steps: int = 24) -> list[tuple[float, float]]:
    """二次/三次贝塞尔采样，用来画尾巴和呆毛。"""
    if len(points) == 3:
        (x0, y0), (x1, y1), (x2, y2) = points
        return [
            (
                (1 - t) ** 2 * x0 + 2 * (1 - t) * t * x1 + t**2 * x2,
                (1 - t) ** 2 * y0 + 2 * (1 - t) * t * y1 + t**2 * y2,
            )
            for t in (i / steps for i in range(steps + 1))
        ]
    if len(points) == 4:
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = points
        return [
            (
                (1 - t) ** 3 * x0 + 3 * (1 - t) ** 2 * t * x1 + 3 * (1 - t) * t**2 * x2 + t**3 * x3,
                (1 - t) ** 3 * y0 + 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t**2 * y2 + t**3 * y3,
            )
            for t in (i / steps for i in range(steps + 1))
        ]
    return points


def _tail(p: Painter) -> None:
    curve = _bezier([(700, 900), (990, 820), (960, 570), (820, 520)], 36)
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    count = len(curve)
    for index, (x, y) in enumerate(curve):
        t = index / max(1, count - 1)
        prev = curve[max(0, index - 1)]
        nxt = curve[min(count - 1, index + 1)]
        dx, dy = nxt[0] - prev[0], nxt[1] - prev[1]
        length = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / length, dx / length
        radius = 72 - 44 * t
        left.append((x + nx * radius, y + ny * radius))
        right.append((x - nx * radius, y - ny * radius))
    p.poly(left + list(reversed(right)), HAIR, LINE, 5)
    # 尾巴尖更亮
    p.ell(820, 520, 34, 32, HAIR_LIGHT)
    # 毛流
    p.arc(860, 700, 58, 40, 210, 330, (255, 255, 255, 110), 7)
    p.line([(946, 812), (974, 780)], HAIR_SHADE, 4)
    p.line([(924, 846), (952, 814)], HAIR_SHADE, 4)


def _back_hair(p: Painter) -> None:
    p.ell(500, 500, 235, 300, HAIR_SHADE)
    p.ell(500, 500, 225, 290, HAIR)
    # 两侧长发
    p.poly([(300, 400), (252, 700), (286, 840), (360, 780), (372, 470)], HAIR)
    p.poly([(700, 400), (748, 700), (714, 840), (640, 780), (628, 470)], HAIR)
    p.poly([(300, 400), (252, 700), (286, 840), (310, 800), (300, 470)], HAIR_SHADE)
    p.poly([(700, 400), (748, 700), (714, 840), (690, 800), (700, 470)], HAIR_SHADE)


def _body(p: Painter) -> None:
    # 脖子
    p.box(468, 640, 532, 720, SKIN_SHADE, radius=18)
    # 肩膀和旗袍
    p.poly([(330, 1000), (670, 1000), (636, 742), (364, 742)], DRESS)
    p.ell(364, 770, 62, 58, DRESS)
    p.ell(636, 770, 62, 58, DRESS)
    # 领口
    p.poly([(438, 736), (500, 806), (562, 736), (540, 722), (500, 770), (460, 722)], SKIN)
    p.line([(438, 736), (500, 806), (562, 736)], GOLD, 5)
    # 盘扣
    for y in (820, 868, 916):
        p.ell(500, y, 11, 11, GOLD)
        p.arc(500, y, 20, 14, 200, 340, GOLD, 3)
    # 樱花刺绣
    for cx, cy, scale in ((420, 850, 1.0), (452, 902, 0.75), (398, 922, 0.6)):
        for angle in range(0, 360, 72):
            rad = math.radians(angle)
            p.ell(
                cx + math.cos(rad) * 13 * scale,
                cy + math.sin(rad) * 13 * scale,
                8 * scale,
                8 * scale,
                (255, 170, 190, 255),
            )
        p.ell(cx, cy, 6 * scale, 6 * scale, GOLD)
    # 肩部高光
    p.arc(364, 748, 56, 28, 200, 320, (90, 90, 110, 255), 4)
    p.arc(636, 748, 56, 28, 220, 340, (90, 90, 110, 255), 4)


def _ears(p: Painter) -> None:
    for mirror in (1, -1):
        base_x = 500 + mirror * 140
        outer = [
            (base_x - mirror * 108, 330),
            (base_x - mirror * 74, 162),
            (base_x + mirror * 58, 286),
        ]
        inner = [
            (base_x - mirror * 82, 310),
            (base_x - mirror * 62, 210),
            (base_x + mirror * 30, 286),
        ]
        p.poly(outer, HAIR, LINE, 5)
        p.poly(inner, (255, 186, 202, 255))
        p.line([(base_x - mirror * 66, 200), (base_x - mirror * 54, 248)], HAIR_SHADE, 3)


def _head(p: Painter) -> None:
    p.ell(500, 500, 202, 206, SKIN)


def _buns(p: Painter) -> None:
    for cx in (286, 714):
        p.ell(cx, 306, 62, 58, HAIR, LINE, 4)
        p.ell(cx - 12, 292, 24, 20, HAIR_LIGHT)
        p.arc(cx, 306, 38, 34, 120, 300, HAIR_SHADE, 4)


def _bangs(p: Painter) -> None:
    top = [
        (292, 430),
        (294, 356),
        (326, 306),
        (382, 270),
        (444, 252),
        (500, 248),
        (556, 252),
        (618, 270),
        (674, 306),
        (706, 356),
        (708, 430),
    ]
    bottom = _bezier([(708, 424), (500, 452), (292, 424)], 16)
    p.poly(top + bottom, HAIR)
    # 浅一点的发梢，避免刘海像一条直线
    for cx, half, tip in ((376, 34, 454), (452, 32, 462), (548, 32, 462), (624, 34, 454)):
        p.poly(
            [(cx - half, 430), (cx + half, 430), (cx + half * 0.4, tip), (cx, tip + 6), (cx - half * 0.6, tip - 4)],
            HAIR,
        )
    # 两侧修饰脸型的发束
    for cx, mirror in ((344, 1), (656, -1)):
        half = 46
        p.poly(
            [
                (cx - half * mirror, 420),
                (cx + half * mirror, 420),
                (cx + half * 0.30 * mirror, 468),
                (cx, 474),
                (cx - half * 0.50 * mirror, 462),
            ],
            HAIR,
        )
    # 发丝纹理
    p.line([(360, 386), (420, 330), (470, 300)], HAIR_SHADE, 3)
    p.line([(640, 386), (580, 330), (530, 300)], HAIR_SHADE, 3)
    p.line([(500, 420), (500, 392)], HAIR_SHADE, 2)
    # 侧发
    p.poly([(300, 430), (282, 560), (262, 700), (300, 676), (330, 540), (336, 440)], HAIR)
    p.poly([(700, 430), (718, 560), (738, 700), (700, 676), (670, 540), (664, 440)], HAIR)
    # 高光
    p.arc(500, 360, 150, 110, 200, 340, HAIR_LIGHT, 9)
    p.line([(360, 360), (420, 310)], HAIR_LIGHT, 7)
    p.line([(640, 360), (580, 310)], HAIR_LIGHT, 7)
    # 呆毛
    ahoge = _bezier([(503, 262), (512, 200), (552, 170), (596, 196)], 18)
    for index in range(len(ahoge) - 1):
        p.line([ahoge[index], ahoge[index + 1]], HAIR, 10)
    p.ell(596, 196, 10, 10, HAIR)


def _normal_eyes(p: Painter, *, iris_shift: float = 0, wide: float = 0) -> None:
    for cx in (412, 588):
        p.ell(cx, 516, 52 + wide, 58 + wide, WHITE, LINE, 3)
        p.ell(cx, 520 + iris_shift, 33, 39, EYE)
        p.ell(cx, 520 + iris_shift, 20, 25, EYE_DARK)
        p.ell(cx - 11, 504 + iris_shift, 10, 12, WHITE)
        p.ell(cx + 10, 536 + iris_shift, 5, 6, (255, 255, 255, 220))
        # 上睫毛
        p.arc(cx, 486, 54 + wide, 32, 200, 340, LINE, 9)
        p.poly([(cx - 58, 492), (cx - 76, 474), (cx - 50, 478)], LINE)


def _brows(p: Painter, *, inner: float = 0, outer: float = 0) -> None:
    p.line([(366, 438 + outer), (412, 424 + inner), (458, 436 + outer)], HAIR_SHADE, 8)
    p.line([(542, 436 + outer), (588, 424 + inner), (634, 438 + outer)], HAIR_SHADE, 8)


def _mouth(p: Painter, kind: str) -> None:
    if kind == "smile":
        p.arc(500, 596, 26, 20, 20, 160, LINE, 5)
    elif kind == "cat":
        p.arc(486, 602, 15, 12, 20, 160, LINE, 5)
        p.arc(514, 602, 15, 12, 20, 160, LINE, 5)
    elif kind == "frown":
        p.arc(500, 626, 22, 16, 200, 340, LINE, 5)
    elif kind == "open":
        p.ell(500, 614, 14, 18, (120, 52, 62, 255), LINE, 3)
    elif kind == "small":
        p.ell(500, 606, 7, 9, (120, 52, 62, 255))
    elif kind == "wave":
        p.line([(478, 608), (490, 614), (502, 606), (514, 614), (526, 608)], LINE, 4)
    elif kind == "smirk":
        p.arc(505, 606, 30, 16, 200, 330, LINE, 5)
    elif kind == "flat":
        p.line([(484, 610), (516, 610)], LINE, 4)


def _blush(p: Painter, strength: int = 95, lines: bool = False) -> None:
    alpha = max(0, min(255, strength))

    def draw(layer: ImageDraw.ImageDraw, painter: Painter) -> None:
        for cx in (372, 628):
            layer.ellipse(
                [painter.p(cx - 44, 566), painter.p(cx + 44, 610)],
                fill=(255, 126, 150, alpha),
            )
        if lines:
            for cx in (372, 628):
                for offset in (-18, 0, 18):
                    layer.line(
                        painter.points([(cx + offset - 8, 604), (cx + offset + 4, 576)]),
                        fill=(255, 96, 126, min(255, alpha + 60)),
                        width=max(1, int(3 * painter.k)),
                    )

    p.glow(draw)


def _anger_mark(p: Painter) -> None:
    color = (226, 64, 72, 255)
    p.line([(622, 396), (646, 420)], color, 5)
    p.line([(646, 396), (622, 420)], color, 5)
    p.arc(634, 408, 22, 22, 200, 340, color, 3)


def _tear(p: Painter, x: float, y: float, size: float = 1.0) -> None:
    p.poly(
        [
            (x, y - 26 * size),
            (x + 12 * size, y - 2 * size),
            (x + 8 * size, y + 14 * size),
            (x - 8 * size, y + 14 * size),
            (x - 12 * size, y - 2 * size),
        ],
        (170, 220, 255, 235),
    )
    p.ell(x - 3 * size, y - 6 * size, 3 * size, 4 * size, (240, 250, 255, 255))


def _expression(p: Painter, expression: str) -> None:
    if expression == "happy":
        _brows(p, inner=-8)
        for cx in (412, 588):
            p.arc(cx, 528, 44, 34, 200, 340, LINE, 10)
            p.poly([(cx - 48, 520), (cx - 66, 504), (cx - 42, 508)], LINE)
        _mouth(p, "cat")
        _blush(p, 115, lines=True)
    elif expression == "angry":
        _brows(p, inner=26, outer=-14)
        for cx, direction in ((412, 1), (588, -1)):
            p.ell(cx, 516, 52, 58, WHITE, LINE, 3)
            p.ell(cx, 524, 33, 39, EYE)
            p.ell(cx, 524, 20, 25, EYE_DARK)
            p.ell(cx - 11 * direction, 510, 9, 11, WHITE)
            outer_x = cx - 60 * direction
            inner_x = cx + 60 * direction
            outer_y, inner_y = 500, 522
            p.poly([(outer_x, 490), (inner_x, 490), (inner_x, inner_y), (outer_x, outer_y)], SKIN)
            p.line([(outer_x, outer_y), (inner_x, inner_y)], LINE, 8)
        p.line([(474, 620), (500, 630), (526, 620)], LINE, 5)
        _anger_mark(p)
    elif expression == "sad":
        _brows(p, inner=-22, outer=12)
        for cx in (412, 588):
            p.ell(cx, 520, 50, 56, WHITE, LINE, 3)
            p.ell(cx, 528, 31, 37, EYE)
            p.ell(cx, 528, 19, 24, EYE_DARK)
            p.ell(cx - 10, 514, 9, 11, WHITE)
            p.arc(cx, 500, 52, 30, 200, 340, LINE, 8)
        p.line([(484, 624), (500, 616), (516, 624)], LINE, 5)
        _tear(p, 352, 574, 0.9)
        _blush(p, 55)
    elif expression == "shy":
        _brows(p, inner=4)
        for cx in (412, 588):
            p.ell(cx, 516, 50, 54, WHITE, LINE, 3)
            p.ell(cx, 528, 30, 33, EYE)
            p.ell(cx, 528, 18, 22, EYE_DARK)
            p.ell(cx - 9, 518, 8, 10, WHITE)
            p.poly([(cx - 54, 462), (cx + 54, 462), (cx + 54, 500), (cx - 54, 500)], SKIN)
            p.line([(cx - 54, 500), (cx + 54, 500)], LINE, 8)
        _mouth(p, "wave")
        _blush(p, 170, lines=True)
    elif expression == "sleepy":
        _brows(p, inner=-4)
        for cx in (412, 588):
            p.arc(cx, 520, 42, 26, 20, 160, LINE, 9)
        _mouth(p, "small")
        _blush(p, 55)
        _text(p, "z Z z", 792, 352, 40, (255, 255, 255, 210))
    elif expression == "surprised":
        _brows(p, inner=-18, outer=-6)
        _normal_eyes(p, wide=8)
        _mouth(p, "open")
        _blush(p, 70)
        p.line([(736, 300), (736, 350)], (255, 236, 160, 255), 8)
        p.ell(736, 370, 7, 7, (255, 236, 160, 255))
    elif expression == "smug":
        p.line([(366, 434), (412, 424), (458, 430)], HAIR_SHADE, 8)
        p.line([(542, 420), (588, 408), (634, 424)], HAIR_SHADE, 8)
        # 左眼正常，右眼半睁
        p.ell(412, 516, 52, 58, WHITE, LINE, 3)
        p.ell(412, 522, 33, 39, EYE)
        p.ell(412, 522, 20, 25, EYE_DARK)
        p.ell(401, 508, 10, 12, WHITE)
        p.arc(412, 486, 54, 32, 200, 340, LINE, 9)
        p.ell(588, 516, 50, 56, WHITE, LINE, 3)
        p.ell(588, 526, 31, 35, EYE)
        p.ell(588, 526, 19, 23, EYE_DARK)
        p.ell(577, 514, 9, 11, WHITE)
        p.poly([(538, 462), (638, 462), (638, 516), (538, 500)], SKIN)
        p.line([(538, 500), (638, 516)], LINE, 8)
        _mouth(p, "smirk")
        _blush(p, 75)
    elif expression == "love":
        _brows(p, inner=-10)
        for cx in (412, 588):
            p.ell(cx, 516, 52, 58, WHITE, LINE, 3)
            _heart(p, cx, 522, 30, (226, 68, 96, 255))
            p.ell(cx - 10, 502, 9, 11, WHITE)
        _mouth(p, "smile")
        _blush(p, 150, lines=True)
    elif expression == "cry":
        _brows(p, inner=-24, outer=10)
        for cx, flip in ((412, 1), (588, -1)):
            p.line([(cx - 38 * flip, 492), (cx, 520), (cx - 38 * flip, 548)], LINE, 8)
        _mouth(p, "open")
        for x in (368, 632):
            p.poly([(x, 536), (x + 16, 600), (x + 6, 648), (x - 10, 648), (x - 16, 600)], (170, 220, 255, 220))
            p.ell(x, 650, 10, 12, (170, 220, 255, 235))
        _blush(p, 90)
    elif expression == "blank":
        _brows(p, inner=2)
        for cx in (412, 588):
            p.ell(cx, 520, 13, 15, LINE)
        _mouth(p, "flat")
        _tear(p, 660, 470, 0.7)
    else:  # normal
        _brows(p)
        _normal_eyes(p)
        _mouth(p, "cat")
        _blush(p, 80)


def _heart(p: Painter, cx: float, cy: float, size: float, color: Any) -> None:
    p.ell(cx - size * 0.42, cy - size * 0.25, size * 0.42, size * 0.4, color)
    p.ell(cx + size * 0.42, cy - size * 0.25, size * 0.42, size * 0.4, color)
    p.poly([(cx - size * 0.82, cy - size * 0.1), (cx + size * 0.82, cy - size * 0.1), (cx, cy + size * 0.88)], color)


def _text(
    p: Painter, text: str, x: float, y: float, size: float, fill: Any, *, stroke: int = 0, font_path: str = ""
) -> None:
    if not text:
        return
    font = load_font(font_path, max(8, int(size * p.k)))
    p.d.text(
        p.p(x, y),
        text,
        font=font,
        fill=fill,
        anchor="mm",
        stroke_width=int(stroke * p.k),
        stroke_fill=(30, 30, 40, 220),
    )


def _particles(p: Painter, theme: Theme, rng: random.Random) -> None:
    """在角色周围撒少量前景元素，避开脸部中心。"""
    accent = theme.accent
    for _ in range(26):
        x = rng.uniform(40, 960)
        y = rng.uniform(40, 960)
        if 260 < x < 740 and 260 < y < 700:
            continue
        scale = rng.uniform(0.6, 1.5)
        if theme.particle == "stars":
            _sparkle(p, x, y, 12 * scale, (*accent, 220))
        elif theme.particle == "petals":
            for angle in range(0, 360, 72):
                rad = math.radians(angle)
                p.ell(
                    x + math.cos(rad) * 9 * scale,
                    y + math.sin(rad) * 9 * scale,
                    5 * scale,
                    5 * scale,
                    (255, 150, 185, 210),
                )
            p.ell(x, y, 4 * scale, 4 * scale, (255, 230, 240, 230))
        elif theme.particle == "rain":
            p.line([(x, y), (x - 8 * scale, y + 34 * scale)], (200, 226, 255, 150), 3)
        elif theme.particle == "sparkles":
            _sparkle(p, x, y, 8 * scale, (255, 255, 255, 210))
        else:  # dots
            p.ell(x, y, 3 * scale, 3 * scale, (90, 90, 90, 70))


def _sparkle(p: Painter, cx: float, cy: float, size: float, color: Any) -> None:
    p.poly(
        [
            (cx, cy - size),
            (cx + size * 0.26, cy - size * 0.26),
            (cx + size, cy),
            (cx + size * 0.26, cy + size * 0.26),
            (cx, cy + size),
            (cx - size * 0.26, cy + size * 0.26),
            (cx - size, cy),
            (cx - size * 0.26, cy - size * 0.26),
        ],
        color,
    )


def _moon(p: Painter, theme: Theme) -> None:
    if not theme.moon:
        return
    p.glow_blurred(
        lambda layer, painter: layer.ellipse([painter.p(748, 98), painter.p(932, 282)], fill=(255, 240, 180, 150)),
        26,
    )
    p.ell(840, 190, 62, 62, (255, 250, 222, 255))
    p.ell(822, 170, 24, 24, (255, 253, 238, 120))


def _caption(p: Painter, text: str, theme: Theme, font_path: str) -> None:
    value = " ".join(str(text or "").split())
    if not value:
        return
    value = value[:16]
    size = 54.0
    font = load_font(font_path, int(size * p.k))
    while size > 30 and font.getlength(value) > 470 * p.k:
        size -= 3
        font = load_font(font_path, int(size * p.k))
    width = min(520, max(220, font.getlength(value) / p.k + 90))
    x0, x1 = 500 - width / 2, 500 + width / 2
    y0, y1 = 852, 952
    p.box(x0 + 6, y0 + 8, x1 + 6, y1 + 8, (20, 20, 30, 90), radius=34)
    p.box(x0, y0, x1, y1, (255, 253, 250, 238), (*theme.accent, 255), 4, radius=34)
    p.d.text(p.p(500, 902), value, font=font, fill=(52, 40, 46, 255), anchor="mm")


def render(
    *,
    expression: str = "happy",
    palette: str = "night",
    caption: str = "",
    seed: int | None = None,
    size: int = 768,
    font_path: str = "",
) -> bytes:
    """绘制一张 PNG；同 seed 输出稳定，换 seed 只改装饰元素。"""
    expr = expression if expression in EXPRESSIONS else "happy"
    theme = THEMES.get(palette, THEMES["night"])
    output = max(320, min(1024, int(size or 768)))
    work = output * 3 if output <= 768 else output * 2
    rng = random.Random(int(seed) if seed is not None else random.randrange(1 << 30))
    p = Painter(work)
    p.gradient(theme.top, theme.bottom)
    _moon(p, theme)
    if theme.particle == "dots":
        for y in range(40, 1000, 26):
            for x in range(40, 1000, 26):
                p.ell(x, y, 2.2, 2.2, (80, 80, 80, 28))
    _particles(p, theme, rng)
    _tail(p)
    _back_hair(p)
    _body(p)
    _head(p)
    _bangs(p)
    p.glow_blurred(
        lambda layer, painter: layer.rectangle([painter.p(330, 432), painter.p(670, 458)], fill=(120, 96, 132, 26)),
        10,
    )
    _buns(p)
    _ears(p)
    _expression(p, expr)
    _caption(p, caption, theme, font_path)
    # 圆角卡片
    mask = Image.new("L", (work, work), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, work - 1, work - 1], radius=int(64 * p.k), fill=255)
    p.img.putalpha(mask)
    final = p.img.resize((output, output), Image.LANCZOS)
    buffer = Image.new("RGB", (output, output), (255, 255, 255))
    buffer.paste(final, (0, 0), final)
    from io import BytesIO

    out = BytesIO()
    buffer.save(out, format="PNG")
    return out.getvalue()


__all__ = ["EXPRESSIONS", "PALETTES", "analyze_prompt", "render"]
