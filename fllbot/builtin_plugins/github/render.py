"""GitHub 仓库图片卡片：本地 Pillow 排版，不联网、不执行仓库内容。

工具先把只读接口返回的数据整理成固定结构，再由这里画成一张 PNG：
统一的卡片头部、状态图标、统计标签、真实头像与文件类型图标，适合在群里直接查看。
"""

from __future__ import annotations

import io
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fllbot.plugin import load_font

from .icons import draw_icon

KINDS = (
    "repository",
    "contributors",
    "issues",
    "pull_requests",
    "issue",
    "commit",
    "releases",
    "branches",
)

MIN_WIDTH = 720
MAX_WIDTH = 1200
DEFAULT_WIDTH = 960

MAX_CONTRIBUTORS = 30
MAX_ISSUES = 15
MAX_PULLS = 15
MAX_COMMENTS = 4
MAX_FILES = 15
MAX_RELEASES = 5
MAX_BRANCHES = 20

PAPER = "#f6f8fa"
CARD = "#ffffff"
INK = "#1f2328"
MUTED = "#57606a"
FAINT = "#636c76"
LINE = "#d8dee4"
TRACK = "#eaeef2"
GREEN = "#1a7f37"
GREEN_BG = "#dafbe1"
RED = "#cf222e"
RED_BG = "#ffebe9"
BLUE = "#0969da"
BLUE_BG = "#ddf4ff"
PURPLE = "#8250df"
PURPLE_BG = "#fbefff"
AMBER = "#9a6700"
AMBER_BG = "#fff8c5"
GRAY_BG = "#f6f8fa"

_AVATAR_COLORS = (
    "#0969da",
    "#8250df",
    "#1a7f37",
    "#cf222e",
    "#9a6700",
    "#0550ae",
    "#6639ba",
    "#116329",
)
_TOKEN = re.compile(r"[A-Za-z0-9_@#./:+\-]+|\s+|.")
_FILE_STYLES = {
    "added": ("A", GREEN, GREEN_BG),
    "removed": ("D", RED, RED_BG),
    "modified": ("M", AMBER, AMBER_BG),
    "renamed": ("R", BLUE, BLUE_BG),
    "copied": ("C", BLUE, BLUE_BG),
    "changed": ("C", BLUE, BLUE_BG),
}
_FILE_TYPES = {
    ".cs": ("code", "#8250df"),
    ".py": ("code", "#3572a5"),
    ".js": ("code", "#b79000"),
    ".mjs": ("code", "#b79000"),
    ".cjs": ("code", "#b79000"),
    ".ts": ("code", "#3178c6"),
    ".tsx": ("code", "#3178c6"),
    ".jsx": ("code", "#3178c6"),
    ".java": ("code", "#b07219"),
    ".go": ("code", "#00add8"),
    ".rs": ("code", "#b7410e"),
    ".c": ("code", "#b34a6b"),
    ".h": ("code", "#b34a6b"),
    ".cpp": ("code", "#b34a6b"),
    ".hpp": ("code", "#b34a6b"),
    ".cc": ("code", "#b34a6b"),
    ".php": ("code", "#4f5d95"),
    ".rb": ("code", "#9b111e"),
    ".kt": ("code", "#a97bff"),
    ".kts": ("code", "#a97bff"),
    ".lua": ("code", "#3f6bb5"),
    ".sh": ("code", "#4d5a5e"),
    ".ps1": ("code", "#4d5a5e"),
    ".bat": ("code", "#4d5a5e"),
    ".html": ("code", "#b34a26"),
    ".css": ("code", "#563d7c"),
    ".sql": ("file", "#9a6700"),
    ".md": ("file", "#0969da"),
    ".mdx": ("file", "#0969da"),
    ".rst": ("file", "#0969da"),
    ".txt": ("file", "#0969da"),
    ".json": ("file", "#6e7781"),
    ".jsonc": ("file", "#6e7781"),
    ".yml": ("file", "#6e7781"),
    ".yaml": ("file", "#6e7781"),
    ".toml": ("file", "#6e7781"),
    ".ini": ("file", "#6e7781"),
    ".cfg": ("file", "#6e7781"),
    ".png": ("image", "#8250df"),
    ".jpg": ("image", "#8250df"),
    ".jpeg": ("image", "#8250df"),
    ".gif": ("image", "#8250df"),
    ".webp": ("image", "#8250df"),
    ".svg": ("image", "#8250df"),
    ".zip": ("archive", "#9a6700"),
    ".7z": ("archive", "#9a6700"),
    ".rar": ("archive", "#9a6700"),
    ".tar": ("archive", "#9a6700"),
    ".gz": ("archive", "#9a6700"),
}
_LANGUAGE_COLORS = {
    "C#": "#68217a",
    "C": "#555555",
    "C++": "#b34a6b",
    "CSS": "#563d7c",
    "Go": "#00add8",
    "HTML": "#b34a26",
    "Java": "#b07219",
    "JavaScript": "#b79000",
    "Kotlin": "#a97bff",
    "Lua": "#3f6bb5",
    "PHP": "#4f5d95",
    "Python": "#3572a5",
    "Ruby": "#9b111e",
    "Rust": "#b7410e",
    "Shell": "#4d5a5e",
    "TypeScript": "#3178c6",
}


@dataclass(frozen=True)
class Chip:
    text: str
    fg: str = INK
    bg: str = GRAY_BG
    icon: str = ""
    icon_color: str = ""


def _count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _when(value: Any, *, with_time: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return text[:10]
    return moment.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def _repo_label(value: Any) -> str:
    text = str(value or "").strip()
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def _branch_label(value: Any) -> str:
    text = str(value or "").strip()
    return text.split(":", 1)[1] if ":" in text else text


def _avatar_color(name: str) -> str:
    return _AVATAR_COLORS[sum(ord(char) for char in name) % len(_AVATAR_COLORS)]


def _initial(name: str) -> str:
    for char in name.strip():
        if not char.isspace():
            return char.upper() if char.isascii() else char
    return "?"


def _state_style(state: Any, *, merged: bool = False, draft: bool = False) -> tuple[str, str, str, str]:
    """Return label, foreground, background and icon name for a state badge."""
    if draft:
        return "草稿", MUTED, GRAY_BG, "draft"
    if merged:
        return "已合并", PURPLE, PURPLE_BG, "merge"
    value = str(state or "").strip().lower()
    if value == "open":
        return "打开", GREEN, GREEN_BG, "issue"
    if value == "closed":
        return "已关闭", RED, RED_BG, "issue_closed"
    return str(state or "-"), MUTED, GRAY_BG, "dot"


def _state_word(state: Any) -> str:
    return {"open": "打开", "closed": "已关闭", "all": "全部"}.get(
        str(state or "").strip().lower(), str(state or "").strip() or "-"
    )


def _file_style(filename: str) -> tuple[str, str]:
    name = str(filename or "").strip().lower()
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    return _FILE_TYPES.get(suffix, ("file", "#6e7781"))


def _language_color(language: Any) -> str:
    return _LANGUAGE_COLORS.get(str(language or "").strip(), "#6e7781")


def _wrap(text: Any, font, width: int, max_lines: int) -> list[str]:
    """按中英混排折行并保留显式换行；超过 max_lines 时在末行加省略号。"""
    content = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content or width <= 0 or max_lines <= 0:
        return []
    lines: list[str] = []
    truncated = False
    for raw in content.split("\n"):
        paragraph = re.sub(r"\s+", " ", raw).strip()
        if not paragraph:
            continue
        remaining = max_lines - len(lines)
        if remaining <= 0:
            truncated = True
            break
        chunk, cut = _wrap_paragraph(paragraph, font, width, remaining)
        lines.extend(chunk)
        if cut:
            truncated = True
            break
    if not lines:
        return []
    if truncated:
        last = lines[-1]
        while last and font.getlength(last + "…") > width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _wrap_paragraph(text: str, font, width: int, max_lines: int) -> tuple[list[str], bool]:
    lines: list[str] = []
    current = ""
    cut = False
    for token in _TOKEN.findall(text):
        if token.isspace():
            if current and not current.endswith(" "):
                current += " "
            continue
        candidate = current + token
        if font.getlength(candidate.rstrip()) <= width:
            current = candidate
            continue
        if current.strip():
            lines.append(current.rstrip())
            current = ""
            if len(lines) >= max_lines:
                cut = True
                break
        for char in token:
            if current and font.getlength(current + char) > width:
                lines.append(current)
                current = ""
                if len(lines) >= max_lines:
                    cut = True
                    break
            current += char
        if cut:
            break
    if not cut and current.strip():
        lines.append(current.rstrip())
    return lines, cut


def _bold_paths() -> tuple[str, ...]:
    windir = os.environ.get("WINDIR", "C:/Windows")
    return (
        str(Path(windir) / "Fonts/msyhbd.ttc"),
        str(Path(windir) / "Fonts/msyhbd.ttf"),
        str(Path(windir) / "Fonts/simhei.ttf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    )


def _bold_font(font_path: str, size: int):
    from PIL import ImageFont

    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            pass
    for path in _bold_paths():
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


class _Builder:
    """收集可测量的行块，最后一次性绘制成 PNG。"""

    def __init__(
        self,
        width: int,
        font_path: str,
        avatars: dict[str, bytes] | None = None,
    ) -> None:
        self.width = width
        self.font_path = font_path
        self.avatars = avatars or {}
        self.margin = 12
        self.pad = 32
        self.x = self.margin + self.pad
        self.content_width = self.width - 2 * self.x
        self._fonts: dict[tuple[int, bool], tuple[Any, bool]] = {}
        self._avatar_images: dict[tuple[str, int], Any] = {}
        self._items: list[tuple[int, Callable[[Any, int], None]]] = []
        self._canvas = None
        self.kind_label = "仓库"
        self.title = ""
        self.subtitle = ""
        self.tag = ""
        self.header_avatar = ""
        self.header_avatar_name = ""
        self._header_height = 0
        self._title_lines: list[str] = []
        self._subtitle_lines: list[str] = []
        self._header_indent = 0

    # -- 字体与文本 -------------------------------------------------------- #
    def font(self, size: int, bold: bool = False):
        key = (int(size), bool(bold))
        entry = self._fonts.get(key)
        if entry is None:
            if bold:
                loaded = _bold_font(self.font_path, int(size))
                if loaded is not None:
                    entry = (loaded, True)
                else:
                    entry = (self._regular(int(size)), False)
            else:
                entry = (self._regular(int(size)), True)
            self._fonts[key] = entry
        return entry

    def _regular(self, size: int):
        try:
            return load_font(self.font_path, size)
        except ValueError as exc:
            raise ValueError("找不到可用中文字体，请在 github 插件设置中填写 font_path") from exc

    def text(
        self,
        draw,
        xy: tuple[float, float],
        content: Any,
        *,
        size: int = 18,
        color: str = INK,
        bold: bool = False,
        anchor: str | None = None,
    ) -> None:
        font, real = self.font(size, bold)
        draw.text(
            xy,
            str(content),
            font=font,
            fill=color,
            anchor=anchor,
            stroke_width=0 if (not bold or real) else 1,
            stroke_fill=color,
        )

    # -- 头像 -------------------------------------------------------------- #
    def _avatar_image(self, url: str, size: int):
        if not url:
            return None
        key = (url, int(size))
        cached = self._avatar_images.get(key)
        if cached is not None:
            return cached
        raw = self.avatars.get(url)
        if not raw:
            return None
        from PIL import Image, ImageDraw, ImageOps

        try:
            with Image.open(io.BytesIO(raw)) as source:
                image = source.convert("RGB")
        except (OSError, ValueError):
            return None
        image = ImageOps.fit(image, (size, size), Image.LANCZOS)
        mask = Image.new("L", (size * 4, size * 4), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
        mask = mask.resize((size, size), Image.LANCZOS)
        result = (image, mask)
        self._avatar_images[key] = result
        return result

    def paste_avatar(self, url: str, x: float, y: float, size: int) -> bool:
        if self._canvas is None:
            return False
        data = self._avatar_image(str(url or ""), int(size))
        if data is None:
            return False
        image, mask = data
        self._canvas.paste(image, (round(x), round(y)), mask)
        return True

    def avatar(
        self,
        draw,
        url: str,
        name: str,
        x: float,
        y: float,
        size: int,
        *,
        ring: bool = True,
    ) -> None:
        if self.paste_avatar(url, x, y, size):
            if ring:
                draw.ellipse((x, y, x + size, y + size), outline=LINE, width=1)
            return
        color = _avatar_color(name)
        draw.ellipse((x, y, x + size, y + size), fill=color)
        font, _ = self.font(max(10, round(size * 0.44)), bold=True)
        draw.text(
            (x + size / 2, y + size / 2),
            _initial(name),
            font=font,
            fill="#ffffff",
            anchor="mm",
        )

    # -- 行块 -------------------------------------------------------------- #
    def add_paragraph(
        self,
        text: Any,
        *,
        size: int = 18,
        color: str = INK,
        max_lines: int = 6,
        gap: int = 12,
    ) -> None:
        content = str(text or "").strip()
        if not content:
            return
        font, _ = self.font(size)
        lines = _wrap(content, font, self.content_width, max_lines)
        if not lines:
            return
        line_height = round(size * 1.5)
        height = len(lines) * line_height + gap

        def paint(draw, y: int, *, lines=lines, size=size, color=color, line_height=line_height) -> None:
            for index, line in enumerate(lines):
                self.text(draw, (self.x, y + index * line_height), line, size=size, color=color)

        self._items.append((height, paint))

    def add_section(self, title: str, icon: str = "") -> None:
        def paint(draw, y: int) -> None:
            draw.line((self.x, y + 3, self.width - self.x, y + 3), fill=LINE)
            text_x = self.x
            if icon:
                draw_icon(draw, icon, (self.x, y + 15, self.x + 16, y + 31), MUTED)
                text_x += 23
            self.text(draw, (text_x, y + 23), title, size=15, color=MUTED, bold=True, anchor="lm")

        self._items.append((40, paint))

    def add_stats(self, items: list[tuple[str, str, Any, str]]) -> None:
        column_width = self.content_width / len(items)

        def paint(draw, y: int) -> None:
            right = self.width - self.x
            draw.line((self.x, y, right, y), fill=LINE)
            draw.line((self.x, y + 70, right, y + 70), fill=LINE)
            for index, (icon, label, value, color) in enumerate(items):
                x = round(self.x + index * column_width)
                if index:
                    draw.line((x, y + 15, x, y + 55), fill=LINE)
                    x += 24
                draw_icon(draw, icon, (x, y + 16, x + 16, y + 32), color)
                self.text(draw, (x + 23, y + 24), label, size=15, color=MUTED, anchor="lm")
                self.text(draw, (x, y + 48), _count(value), size=25, bold=True, anchor="lm")

        self._items.append((84, paint))

    def add_chips(
        self,
        chips: list[Chip],
        *,
        size: int = 16,
        gap: int = 8,
        row_gap: int = 8,
        bottom: int = 12,
    ) -> None:
        items = [chip for chip in chips if str(chip.text or "").strip()]
        if not items:
            return
        font, _ = self.font(size)
        icon_size = round(size * 0.9)
        pill_height = round(size * 1.35) + 10
        widths = [
            round(font.getlength(chip.text)) + 24 + (icon_size + 5 if chip.icon else 0)
            for chip in items
        ]
        rows: list[list[int]] = []
        current: list[int] = []
        used = 0
        for index, item_width in enumerate(widths):
            extra = item_width if not current else gap + item_width
            if current and used + extra > self.content_width:
                rows.append(current)
                current, used, extra = [], 0, item_width
            current.append(index)
            used += extra
        if current:
            rows.append(current)
        height = len(rows) * pill_height + (len(rows) - 1) * row_gap + bottom

        def paint(draw, y: int) -> None:
            for row_index, row in enumerate(rows):
                x = self.x
                row_y = y + row_index * (pill_height + row_gap)
                for index in row:
                    chip = items[index]
                    draw.rounded_rectangle(
                        (x, row_y, x + widths[index], row_y + pill_height),
                        radius=8,
                        fill=chip.bg,
                        outline=LINE if chip.bg == GRAY_BG else None,
                    )
                    text_x = x + 12
                    if chip.icon:
                        draw_icon(
                            draw,
                            chip.icon,
                            (text_x, row_y + (pill_height - icon_size) / 2,
                             text_x + icon_size, row_y + (pill_height + icon_size) / 2),
                            chip.icon_color or chip.fg,
                        )
                        text_x += icon_size + 5
                    self.text(draw, (text_x, row_y + 5), chip.text, size=size, color=chip.fg)
                    x += widths[index] + gap

        self._items.append((height, paint))

    def add_facts(
        self,
        parts: list[tuple[str, str]],
        *,
        size: int = 15,
        color: str = MUTED,
        gap: int = 18,
        bottom: int = 10,
    ) -> None:
        items = [(str(icon), str(text)) for icon, text in parts if str(text or "").strip()]
        if not items:
            return
        font, _ = self.font(size)
        icon_size = 14
        prepared: list[tuple[str, str]] = []
        for icon, text in items:
            available = self.content_width - (icon_size + 5 if icon else 0)
            lines = _wrap(text, font, max(60, available), 1)
            prepared.append((icon, lines[0] if lines else text))
        items = prepared
        widths = [
            round(font.getlength(text)) + (icon_size + 5 if icon else 0) for icon, text in items
        ]
        rows: list[list[int]] = []
        current: list[int] = []
        used = 0
        for index, item_width in enumerate(widths):
            extra = item_width if not current else gap + item_width
            if current and used + extra > self.content_width:
                rows.append(current)
                current, used, extra = [], 0, item_width
            current.append(index)
            used += extra
        if current:
            rows.append(current)
        line_height = round(size * 1.6)
        height = len(rows) * line_height + bottom

        def paint(draw, y: int) -> None:
            for row_index, row in enumerate(rows):
                x = self.x
                row_y = y + row_index * line_height
                for index in row:
                    icon, text = items[index]
                    if icon:
                        draw_icon(draw, icon, (x, row_y + 2, x + icon_size, row_y + 2 + icon_size), color)
                        x += icon_size + 5
                    self.text(draw, (x, row_y), text, size=size, color=color)
                    x += widths[index] - (icon_size + 5 if icon else 0) + gap

        self._items.append((height, paint))

    def add_row(self, height: int, paint: Callable[[Any, int], None]) -> None:
        self._items.append((int(height), paint))

    # -- 头部与输出 -------------------------------------------------------- #
    def _layout_header(self) -> None:
        title_font, _ = self.font(38, bold=True)
        subtitle_font, _ = self.font(17)
        indent = 64 if self.header_avatar else 0
        self._header_indent = indent
        width = self.content_width - indent
        self._title_lines = _wrap(self.title, title_font, width, 2)
        self._subtitle_lines = (
            _wrap(self.subtitle, subtitle_font, width, 2) if self.subtitle else []
        )
        height = 59 + len(self._title_lines) * 48
        if self._subtitle_lines:
            height += 8 + len(self._subtitle_lines) * 25
        self._header_height = height + 8

    def _paint_header(self, draw) -> None:
        top = self.margin + 22
        draw_icon(draw, "mark-github", (self.x, top, self.x + 22, top + 22), INK)
        context_x = self.x + 32
        context_font, _ = self.font(16, bold=True)
        context = _wrap(self.tag or self.kind_label, context_font, self.content_width - 60, 1)
        if context:
            self.text(draw, (context_x, top + 11), context[0], size=16,
                      color=MUTED, bold=True, anchor="lm")
        draw_icon(draw, self.kind_icon,
                  (self.width - self.x - 20, top + 1, self.width - self.x, top + 21), MUTED)
        title_y = top + 36
        title_x = self.x
        if self._header_indent:
            self.avatar(
                draw,
                self.header_avatar,
                self.header_avatar_name or self.title,
                self.x,
                title_y - 2,
                48,
            )
            title_x += self._header_indent
            title_y += 2
        for line in self._title_lines:
            self.text(draw, (title_x, title_y), line, size=38, color=BLUE if self.kind_icon == "repo" else INK, bold=True)
            title_y += 48
        subtitle_y = title_y + 6
        for line in self._subtitle_lines:
            self.text(draw, (title_x, subtitle_y), line, size=17, color=MUTED)
            subtitle_y += 25

    def _paint_footer(self, draw, height: int) -> None:
        rule_y = height - self.margin - 41
        draw.line((self.x, rule_y, self.width - self.x, rule_y), fill=LINE)
        self.text(draw, (self.width - self.x, rule_y + 19), time.strftime("%Y-%m-%d %H:%M"),
                  size=13, color=MUTED, anchor="rm")

    def render(self) -> bytes:
        from PIL import Image, ImageDraw

        self._layout_header()
        content_height = sum(height for height, _ in self._items)
        height = self.margin * 2 + self._header_height + content_height + 56
        image = Image.new("RGB", (self.width, height), PAPER)
        self._canvas = image
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (self.margin, self.margin, self.width - self.margin, height - self.margin),
            radius=18,
            fill=CARD,
            outline=LINE,
            width=1,
        )
        self._paint_header(draw)
        y = self.margin + self._header_height
        for item_height, paint in self._items:
            paint(draw, y)
            y += item_height
        self._paint_footer(draw, height)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        self._canvas = None
        return buffer.getvalue()


def _repository(builder: _Builder, payload: dict[str, Any]) -> None:
    builder.kind_label = "GitHub"
    builder.title = str(payload.get("full_name") or "GitHub 仓库")
    builder.header_avatar = str(payload.get("owner_avatar") or "")
    builder.header_avatar_name = str(payload.get("owner") or "")
    description = str(payload.get("description") or "").strip()
    if description:
        builder.add_paragraph(description, size=19, max_lines=4, gap=16)
    builder.add_stats([
        ("star", "Star", payload.get("stars"), AMBER),
        ("fork", "Fork", payload.get("forks"), BLUE),
        ("issue", "开放 Issue", payload.get("open_issues"), GREEN),
    ])
    chips = []
    language = str(payload.get("language") or "").strip()
    if language:
        chips.append(Chip(language, INK, GRAY_BG, "dot", _language_color(language)))
    if payload.get("license"):
        chips.append(Chip(str(payload["license"]), INK, GRAY_BG, "license"))
    if payload.get("archived"):
        chips.append(Chip("已归档", RED, RED_BG, "archive"))
    builder.add_chips(chips)
    topics = [str(topic) for topic in payload.get("topics") or [] if str(topic).strip()][:10]
    if topics:
        builder.add_chips([Chip(f"#{topic}", BLUE, BLUE_BG) for topic in topics])
    facts: list[tuple[str, str]] = []
    if payload.get("default_branch"):
        facts.append(("branch", f"默认分支 {payload['default_branch']}"))
    if payload.get("pushed_at"):
        facts.append(("clock", f"最近推送 {_when(payload['pushed_at'])}"))
    if payload.get("created_at"):
        facts.append(("calendar", f"创建于 {_when(payload['created_at'])}"))
    builder.add_facts(facts)
    directories = [item for item in payload.get("directories") or [] if isinstance(item, dict)][:8]
    if directories:
        builder.add_section("主要目录", "folder")
        builder.add_chips(
            [
                Chip(f"{item.get('path')}/ · {_count(item.get('files'))} 文件", INK, GRAY_BG, "folder")
                for item in directories
            ]
        )
    if payload.get("homepage"):
        builder.add_facts([("arrow_right", str(payload["homepage"]))], size=14)


def _contributors(builder: _Builder, payload: dict[str, Any]) -> None:
    builder.kind_label = "贡献者"
    builder.title = "贡献者榜单"
    builder.tag = _repo_label(payload.get("repository"))
    items = [item for item in payload.get("contributors") or [] if isinstance(item, dict)]
    if payload.get("source") == "github_web":
        builder.title = "贡献者"
        builder.subtitle = "来源 GitHub 仓库首页 · 名单可能不完整 · 网页未提供提交数"
        for item in items[:MAX_CONTRIBUTORS]:
            builder.add_facts([("people", str(item.get("login") or ""))], size=20)
        return
    pending = str(payload.get("state") or "").lower() == "pending"
    if pending:
        builder.subtitle = "GitHub 仍在统计该仓库的贡献者"
    else:
        builder.subtitle = f"按提交数排序 · 共 {len(items)} 位" + (
            " · 含匿名提交" if payload.get("anonymous_included") else ""
        )
    if pending:
        builder.add_paragraph(
            "GitHub 的贡献者统计接口返回 202（统计中），稍后重新生成即可；也可以用提交记录查看最近作者。",
            size=17,
            color=MUTED,
        )
        return
    if not items:
        builder.add_paragraph("仓库还没有可统计的贡献者，或 GitHub 仍在统计。", size=17, color=MUTED)
        return
    top = max((int(item.get("contributions") or 0) for item in items), default=0) or 1
    builder.add_section("提交排行", "commit")
    shown = items[:MAX_CONTRIBUTORS]
    bar_width = builder.content_width - 100
    for index, item in enumerate(shown):
        login = str(item.get("login") or item.get("name") or "匿名贡献者").strip() or "匿名贡献者"
        count = int(item.get("contributions") or 0)
        ratio = min(1.0, max(0.0, count / top))
        color = _avatar_color(login)
        avatar_url = str(item.get("avatar") or "")
        anonymous = not str(item.get("login") or "").strip()
        count_text = f"{count:,} 次提交"
        available = builder.content_width - 96 - round(builder.font(15)[0].getlength(count_text)) - 18
        name_text = f"{login}（匿名）" if anonymous and "匿名" not in login else login
        name_lines = _wrap(name_text, builder.font(21, bold=True)[0], max(80, available), 1)
        label = name_lines[0] if name_lines else login
        rank = index + 1

        def paint(
            draw,
            y: int,
            *,
            label=label,
            count_text=count_text,
            ratio=ratio,
            color=color,
            avatar_url=avatar_url,
            login=login,
            rank=rank,
        ) -> None:
            rank_color = {1: "#bf8700", 2: "#6e7781", 3: "#9a6700"}.get(rank)
            if rank_color:
                draw.ellipse((builder.x, y + 14, builder.x + 26, y + 40), fill=rank_color)
                draw.text(
                    (builder.x + 13, y + 27),
                    str(rank),
                    font=builder.font(13, bold=True)[0],
                    fill="#ffffff",
                    anchor="mm",
                )
            else:
                builder.text(draw, (builder.x + 6, y + 17), f"{rank:02d}", size=14, color=MUTED)
            builder.avatar(draw, avatar_url, login, builder.x + 38, y + 8, 42)
            builder.text(draw, (builder.x + 92, y + 8), label, size=21, color=INK, bold=True)
            count_width = builder.font(15)[0].getlength(count_text)
            icon_x = builder.x + builder.content_width - count_width - 18
            draw_icon(draw, "commit", (icon_x, y + 14, icon_x + 14, y + 28), MUTED)
            builder.text(
                draw,
                (builder.x + builder.content_width, y + 12),
                count_text,
                size=15,
                color=MUTED,
                anchor="ra",
            )
            draw.rounded_rectangle(
                (builder.x + 92, y + 46, builder.x + 92 + bar_width, y + 54),
                radius=4,
                fill=TRACK,
            )
            filled = round(bar_width * ratio)
            if filled > 0:
                draw.rounded_rectangle(
                    (builder.x + 92, y + 46, builder.x + 92 + max(8, filled), y + 54),
                    radius=4,
                    fill=color,
                )

        builder.add_row(66, paint)
    if len(items) > len(shown):
        builder.add_paragraph(
            f"…还有 {len(items) - len(shown)} 位贡献者未显示，可减少 limit 或直接查看仓库页面。",
            size=14,
            color=MUTED,
            max_lines=1,
            gap=10,
        )


def _state_pill(
    builder: _Builder,
    draw,
    x: float,
    y: float,
    label: str,
    fg: str,
    bg: str,
    icon: str,
    *,
    size: int = 15,
) -> float:
    font, _ = builder.font(size)
    icon_size = 14
    width = round(font.getlength(label)) + 24 + icon_size
    height = round(size * 1.35) + 10
    draw.rounded_rectangle((x, y, x + width, y + height), radius=height // 2, fill=bg)
    draw_icon(draw, icon, (x + 10, y + (height - icon_size) / 2, x + 10 + icon_size,
                           y + (height + icon_size) / 2), fg)
    builder.text(draw, (x + 10 + icon_size + 5, y + 4), label, size=size, color=fg)
    return width


def _issue_row(
    builder: _Builder,
    item: dict[str, Any],
    label: str,
    fg: str,
    icon: str,
) -> tuple[int, Callable[[Any, int], None]]:
    title_font, _ = builder.font(20, bold=True)
    number_font, _ = builder.font(16)
    meta_font, _ = builder.font(15)
    number = item.get("number")
    number_text = f"#{number}"
    title = str(item.get("title") or "").strip() or "（无标题）"
    status_width = round(meta_font.getlength(label))
    number_width = round(number_font.getlength(number_text))
    title_x = builder.x + 32
    right = builder.width - builder.x
    title_right = right - max(number_width, status_width) - 18
    title_lines = _wrap(title, title_font, title_right - title_x, 2)
    meta_parts: list[tuple[str, str]] = []
    if item.get("author"):
        meta_parts.append(("", str(item["author"])))
    if item.get("comments"):
        meta_parts.append(("comment", _count(item["comments"])))
    labels = [str(value) for value in item.get("labels") or []][:3]
    if labels:
        meta_parts.append(("tag", "、".join(labels)))
    if item.get("head") and item.get("base"):
        meta_parts.append(("branch", f"{_branch_label(item['head'])} → {_branch_label(item['base'])}"))
    if item.get("updated_at"):
        meta_parts.append(("clock", f"更新 {_when(item['updated_at'])}"))
    avatar_url = str(item.get("avatar") or "")
    height = max(64, len(title_lines) * 29 + 34)

    def paint(draw, y: int) -> None:
        draw.line((builder.x, y, right, y), fill=LINE)
        draw_icon(draw, icon, (builder.x, y + 13, builder.x + 20, y + 33), fg)
        builder.text(draw, (right, y + 12), number_text, size=16, color=FAINT, anchor="ra")
        builder.text(draw, (right, y + 36), label, size=15, color=fg, anchor="ra")
        line_y = y + 8
        for line in title_lines:
            builder.text(draw, (title_x, line_y), line, size=20, color=INK, bold=True)
            line_y += 26
        meta_y = line_y + 1
        x = title_x
        if avatar_url:
            builder.avatar(draw, avatar_url, str(item.get("author") or ""), x, meta_y - 2, 20)
            x += 26
        for meta_icon, meta_text in meta_parts:
            icon_space = 18 if meta_icon else 0
            available = title_right - x - icon_space
            if available < 36:
                break
            fitted = _wrap(meta_text, meta_font, available, 1)
            if not fitted:
                continue
            if meta_icon:
                draw_icon(draw, meta_icon, (x, meta_y + 2, x + 13, meta_y + 15), FAINT)
            x += icon_space
            builder.text(draw, (x, meta_y), fitted[0], size=15, color=MUTED)
            x += meta_font.getlength(fitted[0]) + 13

    return height, paint


def _issues(builder: _Builder, payload: dict[str, Any]) -> None:
    builder.kind_label = "Issue"
    builder.title = "Issue 列表"
    builder.tag = _repo_label(payload.get("repository"))
    items = [item for item in payload.get("issues") or [] if isinstance(item, dict)]
    state = str(payload.get("state") or "open")
    subtitle = f"状态 {_state_word(state)} · 显示 {len(items)} 条"
    skipped = int(payload.get("pull_requests_skipped") or 0)
    if skipped:
        subtitle += f" · 已过滤 {skipped} 个 PR"
    builder.subtitle = subtitle
    if not items:
        builder.add_paragraph("没有符合条件的 Issue，可换 state 或稍后再看。", size=17, color=MUTED)
        return
    for item in items[:MAX_ISSUES]:
        label, fg, _, icon = _state_style(item.get("state"))
        builder.add_row(*_issue_row(builder, item, label, fg, icon))
    if len(items) > MAX_ISSUES:
        builder.add_paragraph(f"…还有 {len(items) - MAX_ISSUES} 条未显示。", size=14, color=MUTED, max_lines=1, gap=8)


def _pull_requests(builder: _Builder, payload: dict[str, Any]) -> None:
    builder.kind_label = "Pull Request"
    builder.title = "PR 列表"
    builder.tag = _repo_label(payload.get("repository"))
    items = [item for item in payload.get("pull_requests") or [] if isinstance(item, dict)]
    state = str(payload.get("state") or "open")
    builder.subtitle = f"状态 {_state_word(state)} · 显示 {len(items)} 条"
    if not items:
        builder.add_paragraph("没有符合条件的 PR，可换 state 或稍后再看。", size=17, color=MUTED)
        return
    for item in items[:MAX_PULLS]:
        label, fg, _, icon = _state_style(
            item.get("state"), merged=bool(item.get("merged")), draft=bool(item.get("draft"))
        )
        if icon == "issue":
            icon = "pr"
        elif icon == "issue_closed":
            icon = "pr_closed"
        builder.add_row(*_issue_row(builder, item, label, fg, icon))
    if len(items) > MAX_PULLS:
        builder.add_paragraph(f"…还有 {len(items) - MAX_PULLS} 条未显示。", size=14, color=MUTED, max_lines=1, gap=8)


def _commit(builder: _Builder, payload: dict[str, Any]) -> None:
    builder.kind_label = "提交"
    builder.title = str(payload.get("message") or "提交")
    builder.tag = f"提交 {payload.get('short_sha') or ''}".strip()
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    builder.header_avatar = str(author.get("avatar") or "")
    builder.header_avatar_name = str(author.get("login") or author.get("name") or "")
    subtitle: list[str] = []
    name = str(author.get("login") or author.get("name") or "").strip()
    if name:
        subtitle.append(name)
    when = _when(author.get("date"), with_time=True)
    if when:
        subtitle.append(when)
    builder.subtitle = " · ".join(subtitle)
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    chips = [
        Chip(f"{_count(stats.get('additions'))}", GREEN, GREEN_BG, "plus"),
        Chip(f"{_count(stats.get('deletions'))}", RED, RED_BG, "minus"),
        Chip(f"{_count(payload.get('files_total'))} 个文件", INK, GRAY_BG, "file"),
    ]
    if payload.get("patch_truncated"):
        chips.append(Chip("补丁已截断", AMBER, AMBER_BG, "file"))
    builder.add_chips(chips)
    body = str(payload.get("body") or "").strip()
    if body:
        builder.add_paragraph(body, size=17, max_lines=5, gap=14)
    builder.add_section("改动文件", "code")
    files = [item for item in payload.get("files") or [] if isinstance(item, dict)]
    if not files:
        builder.add_paragraph("没有文件改动信息（可能是合并提交，或数据被截断）。", size=16, color=MUTED, gap=10)
        return
    file_font, _ = builder.font(16)
    stat_font, _ = builder.font(15)
    for item in files[:MAX_FILES]:
        status = str(item.get("status") or "modified").lower()
        status_letter, status_fg, status_bg = _FILE_STYLES.get(status, ("?", MUTED, GRAY_BG))
        name_text = str(item.get("filename") or "").strip() or "（未知文件）"
        icon_name, icon_color = _file_style(name_text)
        additions = _count(item.get("additions"))
        deletions = _count(item.get("deletions"))
        lines = _wrap(name_text, file_font, builder.content_width - 190, 1)
        label = lines[0] if lines else "…"

        def paint(
            draw,
            y: int,
            *,
            label=label,
            icon_name=icon_name,
            icon_color=icon_color,
            status_letter=status_letter,
            status_fg=status_fg,
            status_bg=status_bg,
            additions=additions,
            deletions=deletions,
        ) -> None:
            draw.rounded_rectangle((builder.x, y + 8, builder.x + 26, y + 34), radius=6, fill=icon_color)
            draw_icon(draw, icon_name, (builder.x + 6, y + 14, builder.x + 20, y + 28), "#ffffff")
            draw.rounded_rectangle((builder.x + 36, y + 13, builder.x + 54, y + 31), radius=5, fill=status_bg)
            draw.text(
                (builder.x + 45, y + 22),
                status_letter,
                font=builder.font(12, bold=True)[0],
                fill=status_fg,
                anchor="mm",
            )
            builder.text(draw, (builder.x + 64, y + 11), label, size=16, color=INK)
            builder.text(
                draw,
                (builder.x + builder.content_width, y + 12),
                f"+{additions}  −{deletions}",
                size=15,
                color=MUTED,
                anchor="ra",
            )

        builder.add_row(42, paint)
    if len(files) > MAX_FILES:
        builder.add_paragraph(f"…还有 {len(files) - MAX_FILES} 个文件未显示。", size=14, color=MUTED, max_lines=1, gap=8)


def _issue(builder: _Builder, payload: dict[str, Any]) -> None:
    pull = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
    is_pull = bool(payload.get("is_pull_request"))
    number = payload.get("number")
    title = str(payload.get("title") or "").strip() or "（无标题）"
    builder.kind_label = "Pull Request" if is_pull else "Issue"
    builder.title = f"#{number} {title}".strip()
    builder.tag = _repo_label(payload.get("repository"))
    builder.header_avatar = str(payload.get("author_avatar") or "")
    builder.header_avatar_name = str(payload.get("author") or "")
    label, _, _, _ = _state_style(
        payload.get("state"), merged=bool(pull.get("merged")), draft=bool(pull.get("draft"))
    )
    subtitle: list[str] = [label]
    if payload.get("author"):
        subtitle.append(f"作者 {payload['author']}")
    when = _when(payload.get("updated_at") or payload.get("created_at"), with_time=True)
    if when:
        subtitle.append(f"更新 {when}")
    builder.subtitle = " · ".join(subtitle)
    labels = [str(value) for value in payload.get("labels") or []][:6]
    if labels:
        builder.add_chips([Chip(value, BLUE, BLUE_BG, "tag") for value in labels])
    if is_pull:
        chips = [
            Chip(f"{_count(pull.get('additions'))}", GREEN, GREEN_BG, "plus"),
            Chip(f"{_count(pull.get('deletions'))}", RED, RED_BG, "minus"),
            Chip(f"{_count(pull.get('changed_files'))} 个文件", INK, GRAY_BG, "file"),
        ]
        if pull.get("head") and pull.get("base"):
            chips.append(
                Chip(f"{_branch_label(pull['head'])} → {_branch_label(pull['base'])}", BLUE, BLUE_BG, "branch")
            )
        builder.add_chips(chips)
    body = str(payload.get("body") or "").strip()
    if body:
        builder.add_section("正文", "file")
        builder.add_paragraph(body, size=17, max_lines=9, gap=14)
    thread = [item for item in payload.get("recent_comments") or [] if isinstance(item, dict)][:MAX_COMMENTS]
    if thread:
        builder.add_section(f"最近评论（共 {_count(payload.get('comments'))} 条）", "comment")
        for comment in thread:
            author = str(comment.get("author") or "匿名").strip() or "匿名"
            when = _when(comment.get("created_at"), with_time=True)
            text = str(comment.get("body") or "").strip()
            lines = _wrap(text, builder.font(16)[0], builder.content_width - 34, 2)
            avatar_url = str(comment.get("avatar") or "")
            height = 26 + len(lines) * 24 + 16

            def paint(
                draw,
                y: int,
                *,
                author=author,
                when=when,
                lines=lines,
                avatar_url=avatar_url,
            ) -> None:
                builder.avatar(draw, avatar_url, author, builder.x, y, 24)
                builder.text(draw, (builder.x + 32, y + 1), author, size=15, color=BLUE, bold=True)
                if when:
                    author_width = round(builder.font(15, bold=True)[0].getlength(author)) + 44
                    builder.text(draw, (builder.x + author_width, y + 3), when, size=13, color=FAINT)
                for index, line in enumerate(lines):
                    builder.text(draw, (builder.x + 32, y + 24 + index * 24), line, size=16, color=INK)

            builder.add_row(height, paint)
    elif payload.get("comments"):
        builder.add_paragraph(
            f"共有 {_count(payload.get('comments'))} 条评论，评论内容未读取。",
            size=15,
            color=MUTED,
            max_lines=1,
            gap=8,
        )


def _releases(builder: _Builder, payload: dict[str, Any]) -> None:
    builder.kind_label = "版本发布"
    builder.title = "版本与发布"
    builder.tag = _repo_label(payload.get("repository"))
    releases = [item for item in payload.get("releases") or [] if isinstance(item, dict)][:MAX_RELEASES]
    tags = [item for item in payload.get("tags") or [] if isinstance(item, dict)][:MAX_RELEASES]
    if not releases and not tags:
        builder.add_paragraph("该仓库还没有 Release 或标签。", size=17, color=MUTED)
        return
    tag_font, _ = builder.font(22, bold=True)
    meta_font, _ = builder.font(15)
    body_font, _ = builder.font(16)
    if releases:
        builder.subtitle = f"最近 {len(releases)} 个版本"
        for release in releases:
            tag = str(release.get("tag") or "").strip() or "未命名版本"
            name = str(release.get("name") or "").strip()
            when = _when(release.get("published_at"), with_time=False)
            body = str(release.get("body") or "").strip()
            lines = _wrap(body, body_font, builder.content_width, 3) if body else []
            assets = [item for item in release.get("assets") or [] if isinstance(item, dict)]
            assets_text = ""
            if assets:
                names = "、".join(str(item.get("name") or "") for item in assets[:4])
                assets_text = f"附件 {names}" if names else ""
            tag_width = round(tag_font.getlength(tag))
            date_width = round(meta_font.getlength(when)) if when else 0
            name_available = max(80, builder.content_width - tag_width - date_width - 46)
            name_lines = _wrap(name, meta_font, name_available, 1) if name else []
            name_label = name_lines[0] if name_lines else ""
            height = 30 + (len(lines) * 24 if lines else 0) + (24 if assets_text else 0) + 26

            def paint(
                draw,
                y: int,
                *,
                tag=tag,
                name_label=name_label,
                when=when,
                lines=lines,
                assets_text=assets_text,
            ) -> None:
                draw_icon(draw, "tag", (builder.x, y + 4, builder.x + 18, y + 22), AMBER)
                builder.text(draw, (builder.x + 26, y - 2), tag, size=22, color=INK, bold=True)
                tag_x = builder.x + 26 + tag_font.getlength(tag) + 12
                if name_label:
                    builder.text(draw, (tag_x, y + 5), name_label, size=15, color=MUTED)
                if when:
                    builder.text(
                        draw,
                        (builder.x + builder.content_width, y + 7),
                        when,
                        size=15,
                        color=MUTED,
                        anchor="ra",
                    )
                text_y = y + 30
                for line in lines:
                    builder.text(draw, (builder.x, text_y), line, size=16, color=INK)
                    text_y += 24
                if assets_text:
                    draw_icon(draw, "archive", (builder.x, text_y + 2, builder.x + 14, text_y + 16), MUTED)
                    builder.text(draw, (builder.x + 19, text_y), assets_text, size=14, color=MUTED)

            builder.add_row(height, paint)
        return
    builder.subtitle = "没有 Release，以下为最近标签"
    for item in tags:
        name = str(item.get("tag") or "").strip() or "未命名标签"
        commit = str(item.get("commit") or "").strip()
        name_lines = _wrap(name, tag_font, max(80, builder.content_width - 130), 1)
        label = name_lines[0] if name_lines else name

        def paint(draw, y: int, *, label=label, commit=commit) -> None:
            draw_icon(draw, "tag", (builder.x, y + 5, builder.x + 18, y + 23), AMBER)
            builder.text(draw, (builder.x + 26, y + 1), label, size=22, color=INK, bold=True)
            if commit:
                draw_icon(draw, "commit", (builder.x + builder.content_width - 76, y + 11,
                                           builder.x + builder.content_width - 62, y + 25), MUTED)
                builder.text(
                    draw,
                    (builder.x + builder.content_width, y + 10),
                    commit,
                    size=14,
                    color=MUTED,
                    anchor="ra",
                )

        builder.add_row(48, paint)


def _branches(builder: _Builder, payload: dict[str, Any]) -> None:
    builder.kind_label = "分支"
    builder.title = "分支列表"
    builder.tag = _repo_label(payload.get("repository"))
    branches = [item for item in payload.get("branches") or [] if isinstance(item, dict)][:MAX_BRANCHES]
    if not branches:
        builder.add_paragraph("没有读取到分支；仓库可能为空。", size=17, color=MUTED)
        return
    default = str(payload.get("default_branch") or "").strip()
    builder.subtitle = f"默认分支 {default or '-'} · 共 {len(branches)} 个"
    name_font, _ = builder.font(19, bold=True)
    meta_font, _ = builder.font(14)
    builder.add_section("分支", "branch")
    for item in branches:
        name = str(item.get("name") or "").strip() or "（未命名分支）"
        sha = str(item.get("commit") or "").strip()
        protected = bool(item.get("protected"))
        is_default = bool(default) and name == default
        name_lines = _wrap(name, name_font, max(80, builder.content_width - 230), 1)
        label = name_lines[0] if name_lines else name

        def paint(
            draw,
            y: int,
            *,
            label=label,
            sha=sha,
            protected=protected,
            is_default=is_default,
        ) -> None:
            draw_icon(draw, "branch", (builder.x, y + 9, builder.x + 18, y + 27), FAINT)
            builder.text(draw, (builder.x + 26, y + 6), label, size=19, color=INK, bold=True)
            x = builder.x + 26 + name_font.getlength(label) + 12
            if is_default:
                draw.rounded_rectangle((x, y + 6, x + 58, y + 32), radius=13, fill=GREEN_BG)
                draw_icon(draw, "check", (x + 8, y + 12, x + 22, y + 26), GREEN)
                builder.text(draw, (x + 25, y + 9), "默认", size=14, color=GREEN)
                x += 66
            if protected:
                draw.rounded_rectangle((x, y + 6, x + 70, y + 32), radius=13, fill=AMBER_BG)
                draw_icon(draw, "shield", (x + 8, y + 12, x + 22, y + 26), AMBER)
                builder.text(draw, (x + 25, y + 9), "受保护", size=14, color=AMBER)
            if sha:
                draw_icon(draw, "commit", (builder.x + builder.content_width - 74, y + 12,
                                           builder.x + builder.content_width - 60, y + 26), MUTED)
                builder.text(
                    draw,
                    (builder.x + builder.content_width, y + 11),
                    sha,
                    size=14,
                    color=MUTED,
                    anchor="ra",
                )

        builder.add_row(48, paint)
    if len([item for item in payload.get("branches") or [] if isinstance(item, dict)]) > MAX_BRANCHES:
        builder.add_paragraph("…还有更多分支未显示。", size=14, color=MUTED, max_lines=1, gap=8)


_KIND_ICONS = {
    "repository": "repo",
    "contributors": "people",
    "issues": "issue",
    "pull_requests": "pr",
    "issue": "issue",
    "commit": "commit",
    "releases": "package",
    "branches": "branch",
}


_BUILDERS = {
    "repository": _repository,
    "contributors": _contributors,
    "issues": _issues,
    "pull_requests": _pull_requests,
    "issue": _issue,
    "commit": _commit,
    "releases": _releases,
    "branches": _branches,
}


def render_card(
    kind: str,
    payload: dict[str, Any],
    *,
    font_path: str = "",
    width: int = DEFAULT_WIDTH,
    avatars: dict[str, bytes] | None = None,
) -> bytes:
    """Render one GitHub data payload as a PNG card."""
    key = str(kind or "").strip().lower()
    if key not in _BUILDERS:
        raise ValueError(f"不支持的图片类型：{kind}（可用：{'、'.join(KINDS)}）")
    if not isinstance(payload, dict) or not payload:
        raise ValueError("没有可渲染的仓库数据")
    try:
        size = int(width)
    except (TypeError, ValueError):
        raise ValueError("图片宽度必须是整数") from None
    if not MIN_WIDTH <= size <= MAX_WIDTH:
        raise ValueError(f"图片宽度应在 {MIN_WIDTH}–{MAX_WIDTH} 之间")
    builder = _Builder(size, str(font_path or ""), avatars)
    builder.kind_icon = _KIND_ICONS[key]
    if key == "issue" and payload.get("is_pull_request"):
        builder.kind_icon = "pr"
    _BUILDERS[key](builder, payload)
    return builder.render()
