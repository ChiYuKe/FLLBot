"""Plugin: text | Role: bounded local text operations | Metadata: plugin.toml."""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
import unicodedata
from urllib.parse import quote, unquote

from fllbot.rendering.text_image import MAX_CHARS as MAX_CHARS
from fllbot.rendering.text_image import load_font as load_font
from fllbot.rendering.text_image import render as render
from fllbot.rendering.text_image import validate as validate


def process(operation: str, text: str, **options: str) -> dict:
    validate(text)
    if operation == "statistics":
        return {
            "characters": len(text),
            "non_whitespace": sum(not c.isspace() for c in text),
            "lines": len(text.splitlines()),
            "chinese_characters": len(re.findall(r"[\u3400-\u9fff]", text)),
            "latin_words": len(re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", text)),
        }
    if operation == "transform":
        mode = options.get("mode", "trim")
        lines = text.splitlines()
        actions = {
            "trim": lambda: "\n".join(line.strip() for line in lines).strip(),
            "remove_blank_lines": lambda: "\n".join(line for line in lines if line.strip()),
            "deduplicate_lines": lambda: "\n".join(dict.fromkeys(lines)),
            "sort_lines": lambda: "\n".join(sorted(lines)),
            "lower": text.lower,
            "upper": text.upper,
            "normalize": lambda: unicodedata.normalize("NFKC", text),
        }
        if mode not in actions:
            raise ValueError("不支持的文本处理方式")
        result = actions[mode]()
    elif operation == "replace":
        old, new = options.get("old", ""), options.get("new", "")
        validate(old)
        if len(new) > MAX_CHARS:
            raise ValueError("替换内容过长")
        count = text.count(old)
        if len(text) + count * (len(new) - len(old)) > MAX_CHARS:
            raise ValueError("替换结果过长，请分段处理")
        return {"text": text.replace(old, new), "replacements": count}
    elif operation == "json":
        mode = options.get("mode", "pretty")
        if mode not in {"pretty", "compact"}:
            raise ValueError("不支持的 JSON 处理方式")
        try:
            value = json.loads(text, parse_constant=lambda _: _invalid_json())
            result = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2 if mode == "pretty" else None,
                separators=None if mode == "pretty" else (",", ":"),
            )
        except (ValueError, RecursionError) as exc:
            raise ValueError("无效的 JSON 文本") from exc
    elif operation == "codec":
        mode = options.get("mode", "")
        actions = {
            "base64_encode": lambda: base64.b64encode(text.encode()).decode("ascii"),
            "base64_decode": lambda: base64.b64decode(text, validate=True).decode("utf-8"),
            "url_encode": lambda: quote(text, safe=""),
            "url_decode": lambda: unquote(text, errors="strict"),
            "html_escape": lambda: html.escape(text),
            "html_unescape": lambda: html.unescape(text),
        }
        if mode not in actions:
            raise ValueError("不支持的编解码方式")
        try:
            result = actions[mode]()
        except (ValueError, binascii.Error, UnicodeError) as exc:
            raise ValueError("无法解码，请检查输入；Base64 解码结果须为 UTF-8 文本") from exc
    else:
        raise ValueError("不支持的操作")
    if len(result) > MAX_CHARS:
        raise ValueError("处理结果过长，请分段处理")
    return {"text": result}


def _invalid_json():
    raise ValueError("非法 JSON 常量")
