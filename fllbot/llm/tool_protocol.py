"""Normalize native tool envelopes at the model boundary, before execution or delivery."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

# Gateways may expand a DSML separator into multiple spaced ASCII/fullwidth bars.
_MARKER = re.compile(r"<\s*(/?)\s*(?:[|｜]\s*)+DSML\s*(?:[|｜]\s*)+", re.I)
_BLOCK = re.compile(r"<DSML:(tool_calls|calls)\s*>(.*?)</DSML:\1\s*>", re.S)
_INVOKE = re.compile(r"""<DSML:invoke\s+name\s*=\s*(["'])([\w.-]+)\1\s*>(.*?)</DSML:invoke\s*>""", re.S)
_PARAMETER = re.compile(
    r"""<DSML:parameter\s+name\s*=\s*(["'])([\w.-]+)\1(?:\s+string\s*=\s*(["'])(true|false)\3)?\s*>(.*?)</DSML:parameter\s*>""",
    re.S,
)


class ToolProtocolError(ValueError):
    """A native envelope cannot safely be treated as text or executed."""


def normalize_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """Validate the entire batch before callbacks and share IDs with the transcript."""
    if not isinstance(raw, list):
        raise ToolProtocolError("工具调用必须是列表")
    ids: set[str] = set()
    for call in raw:
        if not isinstance(call, dict):
            raise ToolProtocolError("工具调用必须是对象")
        call_id = call.get('id')
        if call_id is not None:
            if not isinstance(call_id, str) or not call_id.strip() or call_id in ids:
                raise ToolProtocolError("工具调用 ID 必须是非空且在批次内唯一的字符串")
            ids.add(call_id)
    calls = []
    for call in raw:
        function = call.get('function')
        if call.get('type', 'function') != 'function' or not isinstance(function, dict):
            raise ToolProtocolError("工具调用 function 结构无效")
        name = function.get('name')
        if not isinstance(name, str) or not name.strip():
            raise ToolProtocolError("工具调用缺少有效名称")
        arguments = function.get('arguments', '{}')
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if not isinstance(arguments, str):
            raise ToolProtocolError("工具参数必须是 JSON 字符串或对象")
        call_id = call.get('id')
        if call_id is None:
            call_id = 'call_' + uuid.uuid4().hex
            while call_id in ids:
                call_id = 'call_' + uuid.uuid4().hex
            ids.add(call_id)
        calls.append({'id': call_id, 'type': 'function',
                      'function': {'name': name, 'arguments': arguments}})
    return calls


def contains_dsml(text: str) -> bool:
    return bool(_MARKER.search(text))


def parse_dsml_tool_calls(content: str, tools: list[dict] | None) -> tuple[str, list[dict[str, Any]]]:
    normalized = _MARKER.sub(lambda match: f"<{match[1]}DSML:", content)
    if normalized == content and not contains_dsml(content):
        return content, []
    definitions = {item.get("function", {}).get("name"): item.get("function", {}) for item in tools or []}
    calls = []
    blocks = list(_BLOCK.finditer(normalized))
    if not blocks:
        raise ToolProtocolError("模型返回的工具调用不完整或格式不兼容，本次未执行")
    for block in blocks:
        body = block[2]
        invokes = list(_INVOKE.finditer(body))
        if not invokes or _INVOKE.sub("", body).strip():
            raise ToolProtocolError("模型返回的工具调用结构无效，本次未执行")
        for invoke in invokes:
            name = invoke[2]
            if name not in definitions:
                aliases = [
                    candidate for candidate in definitions if candidate and candidate.replace("__", "_", 1) == name
                ]
                if len(aliases) != 1:
                    raise ToolProtocolError("模型请求了未授权或无法唯一识别的工具，本次未执行")
                name = aliases[0]
            arguments = {}
            parameter_body = invoke[3]
            for parameter in _PARAMETER.finditer(parameter_body):
                key, is_string, value = parameter[2], parameter[4], parameter[5]
                if key in arguments:
                    raise ToolProtocolError("模型返回了重复的工具参数，本次未执行")
                if is_string is None:
                    prop = definitions[name].get("parameters", {}).get("properties", {}).get(key, {})
                    is_string = "true" if prop.get("type") == "string" else "false"
                try:
                    arguments[key] = value if is_string == "true" else json.loads(value)
                except (ValueError, RecursionError) as exc:
                    raise ToolProtocolError("模型返回了无效的工具参数，本次未执行") from exc
            if _PARAMETER.sub("", parameter_body).strip():
                raise ToolProtocolError("模型返回的工具参数不完整，本次未执行")
            calls.append(
                {
                    "id": "call_" + uuid.uuid4().hex,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                }
            )
    clean = _BLOCK.sub("", normalized)
    if "<DSML:" in clean or "</DSML:" in clean:
        raise ToolProtocolError("模型返回了残缺的工具调用标记，本次未执行")
    clean = re.sub(r"<think>.*?</think>|</?think>", "", clean, flags=re.S).strip()
    return clean, calls
