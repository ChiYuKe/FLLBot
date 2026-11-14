# Plugin: poke | Role: implementation | Metadata: plugin.toml
"""QQ 戳一戳：被戳时按人格回应、自动回戳，并让智能体主动戳人。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fllbot.plugin import Context, Plugin
from fllbot.plugin.context import notice_turn_context

plugin = Plugin.from_manifest(__file__)
plugin.setting(
    "poke_reply",
    type="bool",
    label="被戳一戳时回应",
    default=True,
    description="群友戳机器人时，按当前人格生成一条自然回复；只在已启用群聊模型回复的群里生效。",
    group_scoped=True,
)
plugin.setting(
    "poke_back",
    type="bool",
    label="被戳时戳回去",
    default=True,
    description="回应前先戳一下发起者。受回戳冷却限制，避免和另一位会自动回戳的机器人无限互戳。",
    group_scoped=True,
)
plugin.setting(
    "poke_reply_seconds",
    type="int",
    label="戳一戳回应冷却",
    default=10,
    min=0,
    max=600,
    description="同一群两次戳一戳回应之间的最小间隔；0 表示不限制。冷却内的戳不调用模型。",
    group_scoped=True,
)
plugin.setting(
    "poke_back_seconds",
    type="int",
    label="回戳冷却",
    default=10,
    min=0,
    max=600,
    description="对同一位群友两次自动回戳之间的最小间隔；0 表示不限制。",
    group_scoped=True,
)

# QQ 的戳一戳是 notice.notify.poke：user_id 是戳人者，target_id 是被戳者，没有 message_id。
# 这里把事件包装成一轮普通消息上下文交给 ctx.llm，与群聊共用历史、人格和送达。
POKE_TEXT = "戳了你一下"
_POKE_NAME_TTL = 600.0
_POKE_NAMES: dict[tuple[int, int, int], tuple[str, float]] = {}
POKE_TOOL_COOLDOWN_SECONDS = 5.0


async def _poke_nickname(ctx: Context, user_id: int) -> str:
    """补查戳人者昵称；失败时让上下文退化为 QQ 号。"""
    key = (ctx.self_id, int(ctx.group_id or 0), user_id)
    now = time.monotonic()
    cached = _POKE_NAMES.get(key)
    if cached is not None and now < cached[1]:
        return cached[0]
    info: Any = None
    try:
        info = await asyncio.wait_for(
            ctx.connection.get_group_member_info(int(ctx.group_id), user_id), 2.0
        )
    except Exception:
        return ""
    if not isinstance(info, dict):
        return ""
    name = str(info.get("card") or info.get("nickname") or "").strip()
    if not name:
        return ""
    if len(_POKE_NAMES) >= 512:
        for stale in [item for item, (_, expires) in _POKE_NAMES.items() if expires <= now]:
            _POKE_NAMES.pop(stale, None)
        while len(_POKE_NAMES) >= 512:
            _POKE_NAMES.pop(next(iter(_POKE_NAMES)), None)
    _POKE_NAMES[key] = (name, now + _POKE_NAME_TTL)
    return name


@plugin.on_notice("notify", priority=15)
async def respond_to_poke(ctx: Context) -> None:
    event = ctx.event
    if getattr(event, "post_type", "") != "notice" or not getattr(event, "is_poke", False):
        return
    group_id = ctx.group_id
    poker = int(getattr(event, "user_id", 0) or 0)
    target = int(getattr(event, "target_id", 0) or 0)
    # 只处理群里的「别人戳机器人」；私聊戳、群友互戳和机器人自己发起的戳都不接。
    if group_id is None or poker <= 0 or target != ctx.self_id or poker == ctx.self_id:
        return
    settings = ctx.settings
    reply_enabled = bool(settings.get("poke_reply", True))
    back_enabled = bool(settings.get("poke_back", True))
    if not reply_enabled and not back_enabled:
        return

    if back_enabled:
        seconds = float(settings.get("poke_back_seconds", 10) or 0)
        if ctx.bot.permissions.cooldown_ok(("poke", f"back:{ctx.self_id}:{group_id}", poker), seconds):
            try:
                await ctx.poke(poker)
            except Exception:
                ctx.logger.debug("回戳失败：群 %s 用户 %s", group_id, poker)

    if not reply_enabled or not ctx.llm.available:
        return
    if not ctx.llm.scene_enabled("group", group_id):
        return
    seconds = float(settings.get("poke_reply_seconds", 10) or 0)
    if not ctx.bot.permissions.cooldown_ok(("poke", f"reply:{ctx.self_id}:{group_id}", 0), seconds):
        return

    nickname = await _poke_nickname(ctx, poker)
    poke_ctx = notice_turn_context(ctx, POKE_TEXT, nickname=nickname, user_id=poker)
    poke_ctx.plugin = plugin
    try:
        await ctx.llm.respond(poke_ctx, POKE_TEXT)
    except Exception:
        # 戳一戳只是轻量互动；回复失败不要触发通用错误回复或影响后续事件。
        ctx.logger.exception("戳一戳回应失败：群 %s 用户 %s", group_id, poker)


@plugin.on_ai_tool(
    "poke",
    effect="side_effecting",
    capabilities=("qq.send",),
    title="戳一戳",
    description=(
        "在当前群聊里戳一下指定群成员（QQ 戳一戳），用于对方明确要求或轻松的提醒。"
        "只限当前会话，不能跨群；只有群友明确请求或叫到机器人时才能用，不要连续戳同一个人，也不要用它代替正常回复。"
        "省略 user_id 时戳当前消息的发送者。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "minimum": 1,
                "description": "要戳的群成员 QQ 号；省略则戳当前消息发送者",
            },
        },
        "additionalProperties": False,
    },
    timeout=15,
    max_output_chars=500,
)
async def poke(ctx: Context, user_id: int | None = None) -> dict:
    target = int(user_id or ctx.user_id or 0)
    if target <= 0:
        return {"error": "invalid_target", "message": "需要有效的用户 QQ 号。"}
    if target == ctx.self_id:
        return {"error": "invalid_target", "message": "不能戳机器人自己。"}
    decision = getattr(ctx, "trigger_decision", None)
    if decision is not None and not getattr(decision, "addressed", True):
        return {"error": "addressed_required", "message": "只有群友明确请求或叫到机器人时才能主动戳人。"}
    scene = ctx.group_id if ctx.is_group else ctx.user_id
    key = ("poke", f"tool:{ctx.self_id}:{scene}", target)
    if not ctx.bot.permissions.cooldown_ok(key, POKE_TOOL_COOLDOWN_SECONDS):
        return {"error": "poke_rate_limited", "message": "刚戳过这个成员，请稍后再试。"}
    try:
        await ctx.poke(target)
    except Exception as exc:
        return {"error": "poke_failed", "message": str(exc)[:200] or "戳一戳失败，请稍后再试。"}
    return {"poked": target}
