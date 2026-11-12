"""自动参与判断：在请求模型之前，用确定性信号决定这条群消息值不值得处理。

目标是减少“每条闲聊都调用一次模型”的开销，同时让判断可解释、可复现：
只输出分数与因子，不读取消息正文的历史库，也不替模型决定具体怎么回复。
被 @、唤醒词、明确提问等信号会直接抬高分数；纯语气词、复读和刷屏会降低分数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

QUESTION_MARKERS = (
    "?", "？", "吗", "呢", "怎么", "为什么", "如何", "多少", "哪", "谁", "什么",
    "能不能", "可不可以", "是不是", "有没有", "要不要", "行不行", "咋",
)
LAUGHTER = re.compile(r"^[\s哈呵嘿嘻草xswl2333…\.。!！~～,，、\-—+]+$", re.IGNORECASE)
ACKNOWLEDGE = re.compile(
    r"^(嗯+|哦+|噢+|啊+|好+|ok|okay|收到|在|对|是的|确实|哈哈+|草+|6+|牛|厉害|可以)[\s。.!！~～]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParticipationSignals:
    text: str = ""
    addressed: bool = False
    wake_word: bool = False
    mentions_bot: bool = False
    question: bool = False
    reply_to_bot: bool = False
    media: bool = False
    directed_elsewhere: bool = False
    repeated: bool = False
    seconds_since_bot_reply: float | None = None
    bot_replies_recent: int = 0


@dataclass(frozen=True)
class ParticipationDecision:
    score: int
    threshold: int
    participate: bool
    factors: tuple[tuple[str, int], ...] = ()

    def summary(self) -> dict:
        return {
            "score": self.score,
            "threshold": self.threshold,
            "participate": self.participate,
            "factors": dict(self.factors),
        }


def _has_question(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in QUESTION_MARKERS)


def evaluate(signals: ParticipationSignals, threshold: int = -10) -> ParticipationDecision:
    factors: list[tuple[str, int]] = []

    def add(name: str, delta: int) -> None:
        nonlocal score
        if delta:
            score += delta
            factors.append((name, delta))

    score = 0
    if signals.directed_elsewhere:
        add("directed_elsewhere", -100)
    if signals.addressed:
        add("addressed", 100)
    elif signals.wake_word:
        add("wake_word", 80)
    if signals.reply_to_bot:
        add("reply_to_bot", 30)
    if signals.mentions_bot and not signals.addressed and not signals.wake_word:
        add("mentions_bot", 20)
    if signals.question or _has_question(signals.text):
        add("question", 15)
    if signals.media:
        add("media", 10)
    if signals.seconds_since_bot_reply is not None and signals.seconds_since_bot_reply <= 120:
        add("thread_continuation", 20)
    text = signals.text.strip()
    if signals.repeated:
        add("repeated", -20)
    if LAUGHTER.match(text):
        add("laughter_only", -30)
    elif ACKNOWLEDGE.match(text):
        add("acknowledge_only", -20)
    if len(text) <= 1:
        add("too_short", -15)
    if signals.bot_replies_recent >= 4 and not signals.addressed:
        add("bot_dominant", -15)
    return ParticipationDecision(
        score=score,
        threshold=threshold,
        participate=score >= threshold,
        factors=tuple(factors),
    )


__all__ = ["ParticipationDecision", "ParticipationSignals", "evaluate"]
