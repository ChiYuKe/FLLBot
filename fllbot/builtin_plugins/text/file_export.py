"""Plugin: text | Role: bounded UTF-8 Markdown and offline HTML attachments | Metadata: plugin.toml."""

from __future__ import annotations

import html
import re

from .service import validate

MAX_FILE_BYTES = 2 * 1024 * 1024

# 只允许纯文本/源码类扩展名；不发送可执行文件或脚本启动器，避免群文件变成投放载体。
TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".log", ".csv", ".tsv",
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".java", ".kt", ".kts", ".scala", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".go", ".rs", ".rb", ".php", ".swift", ".m", ".mm", ".dart", ".lua", ".r",
    ".pl", ".ex", ".exs", ".sql", ".sh", ".bash", ".zsh", ".ps1",
    ".json", ".jsonc", ".ipynb", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".html", ".htm", ".css", ".scss", ".less", ".gradle",
})

# 补足大小时追加的行注释前缀；没有行注释的格式退回空行。
_LINE_COMMENTS = {
    ".py": "#", ".pyi": "#", ".sh": "#", ".bash": "#", ".zsh": "#", ".ps1": "#",
    ".rb": "#", ".pl": "#", ".r": "#", ".yaml": "#", ".yml": "#", ".toml": "#",
    ".ini": "#", ".cfg": "#", ".conf": "#", ".sql": "--", ".lua": "--",
    ".c": "//", ".h": "//", ".cc": "//", ".cpp": "//", ".hpp": "//", ".cs": "//",
    ".java": "//", ".kt": "//", ".kts": "//", ".scala": "//", ".swift": "//",
    ".js": "//", ".mjs": "//", ".cjs": "//", ".ts": "//", ".tsx": "//", ".jsx": "//",
    ".go": "//", ".rs": "//", ".dart": "//", ".php": "//", ".gradle": "//",
    ".scss": "//", ".less": "//", ".vue": "//", ".svelte": "//",
}


def _safe_name(filename: str) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("文件名不能为空")
    name = filename.strip()
    if len(name) > 120 or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name) or name.endswith((".", " ")):
        raise ValueError("文件名最多 120 字符，不能包含路径、控制字符或特殊文件名字符")
    if name.startswith(".") or name.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        raise ValueError("不能使用隐藏文件名或系统保留名")
    return name


def attachment_name(filename: str, extension: str) -> str:
    name = _safe_name(filename)
    if not name.lower().endswith(extension):
        if "." in name:
            raise ValueError(f"文件名扩展名必须为 {extension}")
        name += extension
    if len(name) > 120:
        raise ValueError("带扩展名的文件名最多 120 字符")
    return name


def text_file_name(filename: str) -> str:
    """校验代码/文本附件名：必须带受支持的扩展名，不自动改名。"""
    name = _safe_name(filename)
    if "." not in name:
        raise ValueError("文件名必须包含扩展名，例如 main.py")
    extension = "." + name.rsplit(".", 1)[-1].lower()
    if extension not in TEXT_EXTENSIONS:
        raise ValueError(f"不支持的文件类型 {extension}；只允许文本或源码文件")
    return name


def _padding_line(filename: str) -> bytes:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    prefix = _LINE_COMMENTS.get(suffix)
    if prefix:
        return (prefix + " " + "=" * max(1, 78 - len(prefix)) + "\n").encode("utf-8")
    return b"\n"


def pad_text_file(payload: bytes, target_bytes: int, filename: str) -> bytes:
    """只追加注释或空行，把文件补到指定字节数；原内容保持不变。"""
    if target_bytes <= 0 or len(payload) >= target_bytes:
        return payload
    if target_bytes > MAX_FILE_BYTES:
        raise ValueError("目标大小不能超过 2 MiB")
    line = _padding_line(filename)
    padded = payload + line * ((target_bytes - len(payload)) // len(line))
    remainder = target_bytes - len(padded)
    if remainder:
        # 注释行放不下时用空行补齐；两者都是合法空白。
        padded += b"\n" * remainder
    return padded


def export_text_file(text: str, filename: str, target_bytes: int = 0) -> tuple[str, bytes]:
    """原样导出代码/文本，不做 Markdown 渲染或内容改写；可选补足到指定大小。"""
    validate(text)
    name = text_file_name(filename)
    payload = pad_text_file(text.encode("utf-8"), target_bytes, name)
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError("生成的文件超过 2 MiB，请缩短内容")
    return name, payload


def export_file(text: str, kind: str, filename: str, *, format: str = "markdown") -> tuple[str, bytes]:
    validate(text)
    if kind not in {"html", "md"}:
        raise ValueError("文件类型必须是 html 或 md")
    name = attachment_name(filename, "." + kind)
    if kind == "md":
        content = text
    else:
        if format == "markdown":
            from markdown_it import MarkdownIt

            parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
            # Export a self-contained document; never embed images that load remote/local resources.
            parser.renderer.rules["image"] = lambda tokens, index, *args: html.escape(
                "[图片：" + tokens[index].content + "]"
            )
            body = parser.render(text)
        elif format == "plain":
            body = "<pre>" + html.escape(text) + "</pre>"
        else:
            raise ValueError("HTML 排版格式必须是 markdown 或 plain")
        content = (
            '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
            '<meta name="referrer" content="no-referrer">'
            "<title>" + html.escape(name.rsplit(".", 1)[0]) + "</title>"
            "<style>body{max-width:960px;margin:40px auto;padding:0 24px;color:#24292f;"
            "font:16px/1.7 system-ui,sans-serif;overflow-wrap:anywhere}"
            "pre{padding:16px;background:#f6f8fa;white-space:pre-wrap}"
            "code{font-family:monospace}table{border-collapse:collapse;display:block;overflow:auto}"
            "th,td{border:1px solid #d0d7de;padding:6px 12px}"
            "blockquote{border-left:4px solid #d0d7de;margin-left:0;padding-left:16px;color:#57606a}"
            "</style></head><body><main>" + body + "</main></body></html>\n"
        )
    payload = content.encode("utf-8")
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError("生成的文件超过 2 MiB，请缩短内容")
    return name, payload
