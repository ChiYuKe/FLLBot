"""插件 API：``from fllbot.plugin import Plugin``。"""

from __future__ import annotations

from ..rules import (
    all_rules,
    any_rule,
    command,
    endswith,
    fullmatch,
    in_group,
    in_private,
    keyword,
    not_rule,
    regex,
    startswith,
    to_me,
)
from .base import HandlerKind, HandlerSpec, LoopSpec, Plugin
from .context import Context
from .settings import MASK, PluginSettings, SettingField, SettingsView

__all__ = [
    "MASK",
    "Context",
    "HandlerKind",
    "HandlerSpec",
    "LoopSpec",
    "Plugin",
    "PluginSettings",
    "SettingField",
    "SettingsView",
    "all_rules",
    "any_rule",
    "command",
    "endswith",
    "fullmatch",
    "in_group",
    "in_private",
    "keyword",
    "not_rule",
    "regex",
    "startswith",
    "to_me",
]
