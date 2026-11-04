"""对最终请求进行保守预算，保留当前用户输入和完整工具调用关系。"""

from __future__ import annotations

import copy
import json
from typing import Any


def wire_messages(messages) -> list[dict[str, Any]]:
    """Host provenance is never sent as an unsupported model protocol field."""
    return [{key:copy.deepcopy(value) for key,value in message.items() if not key.startswith('_fllbot_')}
            for message in messages]


def request_tokens(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, image_tokens: int = 4096
) -> int:
    """UTF-8 字节上界加协议余量；图片使用可配置的预留量。"""
    entries = wire_messages(messages)
    images = 0
    for message in entries:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    part["image_url"] = "[image]"
                    images += 1
    return (
        len(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        + len(json.dumps(tools or [], ensure_ascii=False).encode("utf-8"))
        + len(entries) * 16
        + 128
        + images * image_tokens
    )


def fit_messages(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    window: int,
    output: int,
    image_tokens: int = 4096,
) -> list[dict[str, Any]]:
    """先删除旧轮次，再缩短本轮工具结果；系统指令和本轮输入不静默截断。"""
    result = copy.deepcopy(messages)
    budget = window - output
    while request_tokens(result, tools, image_tokens) > budget:
        last_user = max((i for i, item in enumerate(result) if item.get("role") == "user"), default=0)
        removable = [i for i in range(last_user) if result[i].get("role") not in {"system", "developer"}
                     and result[i].get('_fllbot_source',{}).get('kind') != 'summary']
        if removable:
            start = removable[0]
            end = next((i for i in range(start + 1, last_user) if result[i].get("role") == "user"), last_user)
            result = [
                item
                for i, item in enumerate(result)
                if not (start <= i < end and item.get("role") not in {"system", "developer"})
            ]
            continue
        summaries = [i for i in range(last_user) if result[i].get('_fllbot_source',{}).get('kind') == 'summary']
        if summaries:
            index = summaries[0]
            summary = result[index]
            prefix = summary['_fllbot_source'].get('prefix','')
            body = summary['content'][len(prefix):]
            if len(body) > 100:
                summary['content'] = prefix + body[:len(body)//2] + '\n[摘要因上下文预算已缩减]'
            else:
                result.pop(index)
            continue
        candidates = [
            item
            for item in result
            if item.get("role") == "tool" and isinstance(item.get("content"), str) and len(item["content"]) > 80
        ]
        if not candidates:
            raise ValueError("本轮输入、系统指令或工具定义超过模型上下文预算，请缩短输入、减少工具或调整模型窗口")
        largest = max(candidates, key=lambda item: len(item["content"]))
        largest["content"] = largest["content"][: len(largest["content"]) // 2] + "\n[工具结果因上下文预算已截断]"
    return result
