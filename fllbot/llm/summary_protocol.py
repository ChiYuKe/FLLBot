"""Validate model summaries; fall back to attributed source excerpts without inventing facts."""

import json


class SummaryFormatError(ValueError):
    pass


def parse_summary(text: str, *, incomplete: bool = False) -> list[dict]:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        document = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise SummaryFormatError("invalid_json") from exc
    topics = document.get("topics") if isinstance(document, dict) else document
    if isinstance(topics, dict):
        topics = [topics]
    if not isinstance(topics, list) or not 1 <= len(topics) <= 6:
        raise SummaryFormatError("invalid_topics")
    result = []
    for topic in topics:
        if not isinstance(topic, dict):
            raise SummaryFormatError("invalid_topic")
        clean = {}
        for field in ("topic", "participants", "facts", "conclusions", "open_questions"):
            value = topic.get(field)
            if value is None and field in {"participants", "conclusions", "open_questions"}:
                value = ""
            if isinstance(value, list) and all(isinstance(part, str) for part in value):
                value = "；".join(value)
            if not isinstance(value, str) or (field in {"topic", "facts"} and not value.strip()):
                raise SummaryFormatError("invalid_fields")
            clean[field] = value[:400]
        status = topic.get("status")
        if not isinstance(status, str) or status not in {"open", "closed"}:
            raise SummaryFormatError("invalid_status")
        result.append({**clean, "status": status, "source_incomplete": incomplete})
    return result


def source_excerpts(sources: list[dict]) -> list[dict]:
    # Explicitly labelled raw excerpts are not a semantic summary. Originals stay
    # in storage for recall; advancing this batch avoids blocking later sessions.
    chosen = sources if len(sources) <= 6 else sources[:3] + sources[-3:]
    return [{
        "topic": "原文摘录（自动摘要不可用，需回查完整记录）",
        "participants": "",
        "facts": f"[记忆消息 {item['id']}] " + item["message"]["content"][:350],
        "conclusions": "未生成可靠的话题结论；以下仅为部分原文。",
        "open_questions": "",
        "status": "open",
        "source_incomplete": True,
        "summary_kind": "source_excerpt",
    } for item in chosen]
