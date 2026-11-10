"""Plugin: text | Role: bounded UTF-8 Markdown and offline HTML attachments | Metadata: plugin.toml."""

from __future__ import annotations

import html
import re

from .service import validate

MAX_FILE_BYTES = 2 * 1024 * 1024


def attachment_name(filename: str, extension: str) -> str:
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
    if not name.lower().endswith(extension):
        if "." in name:
            raise ValueError(f"文件名扩展名必须为 {extension}")
        name += extension
    if len(name) > 120:
        raise ValueError("带扩展名的文件名最多 120 字符")
    return name


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
