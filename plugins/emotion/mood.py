# Plugin: emotion | Role: mood state and local signals | Metadata: plugin.toml
"""Per-group mood state, labels and deterministic text signals.

The model is optional: these rules keep the bot emotional even when no LLM is
configured. Message text is inspected in memory only and never written here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

POSITIVE_WORDS = (
    "谢谢",
    "感谢",
    "辛苦了",
    "好棒",
    "太棒",
    "厉害",
    "可爱",
    "喜欢",
    "爱你",
    "抱抱",
    "摸摸",
    "聪明",
    "有用",
    "靠谱",
    "好耶",
    "不错",
    "笑死",
    "哈哈",
)
NEGATIVE_WORDS = (
    "笨",
    "蠢",
    "傻",
    "滚",
    "闭嘴",
    "烦",
    "讨厌",
    "垃圾",
    "废物",
    "没用",
    "别吵",
    "吵死",
    "人工智障",
    "复读机",
    "恶心",
    "无语",
    "离谱",
    "坏蛋",
)

_LAUGH = re.compile(r"(?:哈|呵|嘿|嘻){2,}|233+|hhh+", re.IGNORECASE)
_REPEATED_MARK = re.compile(r"([!?！？。.~～])\1{2,}")

_EMOJI = {
    "兴奋": "🤩",
    "开心": "😊",
    "来劲": "😏",
    "平静": "🙂",
    "不爽": "😒",
    "炸毛": "😤",
    "低落": "😔",
    "困倦": "😪",
}

_TONE = {
    "兴奋": "话多、反应快，愿意接梗",
    "开心": "轻快、愿意多聊两句",
    "来劲": "精神不错，喜欢逗人",
    "平静": "松弛、自然，不端着",
    "不爽": "话少、有点嘴硬，但不伤人",
    "炸毛": "语气冲一点、爱吐槽，但不骂人也不攻击人",
    "低落": "安静、有点丧，回复偏短",
    "困倦": "慢半拍、句子短，偶尔犯迷糊",
}


@dataclass
class Mood:
    """A bot account's mood inside one group."""

    self_id: int
    group_id: int
    valence: float = 0.12
    energy: float = 0.55
    updated_at: float = 0.0
    last_reason: str = ""
    last_event_at: float = 0.0

    def decay(
        self,
        now: float,
        *,
        base_valence: float = 0.12,
        base_energy: float = 0.55,
        half_life_minutes: float = 45.0,
    ) -> None:
        """Pull the mood back to its baseline with a half-life, in place."""
        if self.updated_at <= 0 or now <= self.updated_at:
            self.updated_at = now
            return
        half_life = max(60.0, float(half_life_minutes) * 60.0)
        factor = 0.5 ** ((now - self.updated_at) / half_life)
        self.valence = base_valence + (self.valence - base_valence) * factor
        self.energy = base_energy + (self.energy - base_energy) * factor
        self.updated_at = now

    @property
    def label(self) -> str:
        if self.energy <= 0.22:
            return "困倦"
        if self.valence <= -0.45:
            return "炸毛" if self.energy >= 0.5 else "低落"
        if self.valence >= 0.55 and self.energy >= 0.6:
            return "兴奋"
        if self.valence <= -0.2:
            return "不爽"
        if self.valence >= 0.3:
            return "开心"
        if self.energy >= 0.78:
            return "来劲"
        return "平静"

    @property
    def emoji(self) -> str:
        return _EMOJI[self.label]

    @property
    def tone(self) -> str:
        return _TONE[self.label]

    @property
    def valence_percent(self) -> int:
        return max(0, min(100, round((self.valence + 1.0) * 50.0)))

    @property
    def energy_percent(self) -> int:
        return max(0, min(100, round(self.energy * 100.0)))

    def summary(self) -> dict:
        return {
            "mood": self.label,
            "emoji": self.emoji,
            "tone": self.tone,
            "valence": round(self.valence, 2),
            "energy": round(self.energy, 2),
            "last_reason": self.last_reason,
            "updated_at": self.updated_at,
        }


def judge(text: str, *, addressed: bool = False, hour: int = -1, repeated: bool = False) -> tuple[float, float, str]:
    """Return ``(valence_delta, energy_delta, reason)`` for one group message."""
    value = text or ""
    valence = 0.0
    energy = 0.0
    reason = ""

    positive = sum(1 for word in POSITIVE_WORDS if word in value)
    negative = sum(1 for word in NEGATIVE_WORDS if word in value)
    if positive:
        valence += min(positive, 3) * 0.04
        energy += min(positive, 2) * 0.02
        reason = "被夸了"
    if negative:
        valence -= min(negative, 3) * 0.05
        # 被气到反而会清醒一点，但心情更差。
        energy += min(negative, 2) * 0.015
        reason = "被凶了"
    if _LAUGH.search(value):
        valence += 0.02
        energy += 0.01
        reason = reason or "被逗笑了"
    if _REPEATED_MARK.search(value):
        valence -= 0.02
        energy -= 0.01
        reason = reason or "被刷屏了"
    if addressed:
        valence += 0.03
        energy += 0.02
        reason = reason or "被叫到了"
    if repeated:
        valence -= 0.04
        energy -= 0.01
        reason = "被复读刷屏了"
    if 0 <= hour < 6:
        valence -= 0.015
        energy -= 0.03
        reason = reason or "深夜犯困"

    return valence, energy, reason


__all__ = ["Mood", "judge"]
