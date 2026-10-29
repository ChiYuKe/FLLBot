"""群治理：入群欢迎、违禁词、刷屏检测。

配置项都可以按群覆盖（`group_scoped=True`），管理端可通过
`GET/PATCH /api/plugins/group_guard/settings` 读写。
"""

from __future__ import annotations

import re
import time
from collections import deque

from fllbot.llm.personas import render_template
from fllbot.onebot.models import NoticeEvent
from fllbot.plugin import Context, Plugin

plugin = Plugin(name="group_guard", version="0.2.0", description="入群欢迎、违禁词与刷屏治理")

plugin.setting("welcome_enabled", type="bool", label="入群欢迎", default=True, group_scoped=True)
plugin.setting(
    "welcome_template",
    type="text",
    label="欢迎语",
    default="欢迎 {nickname} 加入本群～",
    description="可用占位符：{nickname} {user_id} {group_id}",
    group_scoped=True,
)
plugin.setting("farewell_enabled", type="bool", label="退群提示", default=False, group_scoped=True)
plugin.setting(
    "farewell_template",
    type="text",
    label="退群语",
    default="{nickname} 离开了群聊",
    group_scoped=True,
)
plugin.setting(
    "badwords",
    type="list",
    label="违禁词",
    default=[],
    description="每行一个；以 re: 开头按正则匹配",
    group_scoped=True,
)
plugin.setting(
    "badword_action",
    type="select",
    label="违禁词处理",
    default="delete",
    options=["delete", "delete_and_mute", "warn"],
    group_scoped=True,
)
plugin.setting(
    "badword_mute_seconds",
    type="int",
    label="违禁词禁言秒数",
    default=600,
    min=60,
    max=2592000,
    group_scoped=True,
)
plugin.setting("spam_enabled", type="bool", label="刷屏检测", default=True, group_scoped=True)
plugin.setting(
    "spam_window",
    type="float",
    label="刷屏检测窗口（秒）",
    default=8.0,
    min=1,
    max=300,
    group_scoped=True,
)
plugin.setting(
    "spam_limit",
    type="int",
    label="窗口内允许条数",
    default=6,
    min=2,
    max=200,
    group_scoped=True,
)
plugin.setting(
    "spam_action",
    type="select",
    label="刷屏处理",
    default="delete_and_mute",
    options=["delete", "delete_and_mute", "warn"],
    group_scoped=True,
)
plugin.setting(
    "spam_mute_seconds",
    type="int",
    label="刷屏禁言秒数",
    default=300,
    min=60,
    max=2592000,
    group_scoped=True,
)

_ACTION_LABELS = {
    "delete": "已撤回",
    "delete_and_mute": "已撤回并禁言",
    "warn": "警告",
}

# (group_id, user_id) -> 最近的发言时间
_recent: dict[tuple[int, int], deque[float]] = {}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def _render(template: str, ctx: Context, user_id: int) -> str:
    return render_template(
        template or "",
        {
            "nickname": ctx.nickname if ctx.user_id == user_id else str(user_id),
            "user_id": user_id,
            "group_id": ctx.group_id or "",
        },
    ).strip()


def _match_badword(text: str, words: list[str]) -> str | None:
    lowered = text.lower()
    for word in words:
        word = str(word).strip()
        if not word:
            continue
        if word.lower().startswith("re:"):
            try:
                if re.search(word[3:], text, re.IGNORECASE):
                    return word
            except re.error:
                continue
        elif word.lower() in lowered:
            return word
    return None


def _is_command(ctx: Context) -> bool:
    text = ctx.text
    if not text:
        return False
    return any(prefix and text.startswith(prefix) for prefix in ctx.config.bot.command_prefix)


async def _punish(ctx: Context, action: str, mute_seconds: int, reason: str) -> None:
    """按配置撤回 / 禁言 / 仅警告。"""
    if action != "warn":
        try:
            await ctx.delete()
        except Exception:
            ctx.logger.debug("撤回消息失败（机器人可能不是管理员）", exc_info=True)

    muted = False
    if action == "delete_and_mute" and ctx.group_id and ctx.user_id:
        try:
            await ctx.connection.set_group_ban(int(ctx.group_id), ctx.user_id, mute_seconds)
            muted = True
        except Exception:
            ctx.logger.debug("禁言失败（机器人可能不是管理员）", exc_info=True)

    label = _ACTION_LABELS.get(action, "已处理")
    detail = f"{label}：{reason}"
    if muted:
        detail += f"，禁言 {_humanize_seconds(mute_seconds)}"
    try:
        await ctx.reply(detail)
    except Exception:
        ctx.logger.debug("发送处理提示失败", exc_info=True)


def _humanize_seconds(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds // 86400} 天"
    if seconds >= 3600:
        return f"{seconds // 3600} 小时"
    if seconds >= 60:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


# --------------------------------------------------------------------------- #
# 处理器
# --------------------------------------------------------------------------- #


@plugin.on_message(priority=5)
async def guard_message(ctx: Context) -> None:
    if not ctx.is_group or not ctx.group_id:
        return
    # 命令交给业务插件，不做内容审核与刷屏统计
    if _is_command(ctx):
        return

    text = ctx.text
    if text:
        words = ctx.settings.get("badwords") or []
        hit = _match_badword(text, words)
        if hit is not None:
            action = str(ctx.settings.get("badword_action") or "delete")
            mute_seconds = int(ctx.settings.get("badword_mute_seconds") or 600)
            await _punish(ctx, action, mute_seconds, f"命中违禁词「{hit.lstrip('re:')[:20]}」")
            ctx.stop()
            return

    if ctx.settings.get("spam_enabled"):
        if await _check_spam(ctx):
            ctx.stop()


async def _check_spam(ctx: Context) -> bool:
    window = float(ctx.settings.get("spam_window") or 8.0)
    limit = int(ctx.settings.get("spam_limit") or 6)
    key = (int(ctx.group_id or 0), ctx.user_id)
    now = time.monotonic()

    history = _recent.setdefault(key, deque())
    while history and now - history[0] > window:
        history.popleft()
    history.append(now)

    if len(_recent) > 2000:
        for old_key in [item for item, times in _recent.items() if not times]:
            _recent.pop(old_key, None)

    if len(history) < limit:
        return False

    history.clear()
    action = str(ctx.settings.get("spam_action") or "delete_and_mute")
    mute_seconds = int(ctx.settings.get("spam_mute_seconds") or 300)
    await _punish(ctx, action, mute_seconds, f"疑似刷屏：{int(window)} 秒内发言超过 {limit} 条")
    return True


@plugin.on_notice("group_increase", "group_decrease", priority=10)
async def guard_notice(ctx: Context) -> None:
    event = ctx.event
    if not isinstance(event, NoticeEvent) or not ctx.group_id or not event.user_id:
        return

    if event.notice_type == "group_increase" and ctx.settings.get("welcome_enabled"):
        template = str(ctx.settings.get("welcome_template") or "")
        if template:
            await ctx.send(_render(template, ctx, int(event.user_id)))
    elif event.notice_type == "group_decrease" and ctx.settings.get("farewell_enabled"):
        template = str(ctx.settings.get("farewell_template") or "")
        if template:
            await ctx.send(_render(template, ctx, int(event.user_id)))


@plugin.on_command("guard", aliases=("群管",), permission="group_admin", priority=5)
async def guard_status(ctx: Context) -> None:
    settings = ctx.settings
    badwords = settings.get("badwords") or []
    lines = [
        "群治理状态：",
        f"- 入群欢迎：{'开' if settings.get('welcome_enabled') else '关'}"
        f"（模板：{str(settings.get('welcome_template'))[:30]}）",
        f"- 退群提示：{'开' if settings.get('farewell_enabled') else '关'}",
        f"- 违禁词：{len(badwords)} 个，处理方式 {settings.get('badword_action')}",
        f"- 刷屏检测：{'开' if settings.get('spam_enabled') else '关'}"
        f"（{settings.get('spam_window')}s / {settings.get('spam_limit')} 条，处理 {settings.get('spam_action')}）",
    ]
    await ctx.reply("\n".join(lines))


@plugin.on_command("welcome", aliases=("欢迎语",), permission="group_admin", priority=5)
async def welcome_control(ctx: Context) -> None:
    args = ctx.args.strip()
    if not args:
        current = ctx.settings.get("welcome_template")
        await ctx.reply(f"当前欢迎语：{current}\n用法：/welcome on|off|set <模板>")
        return

    sub, _, rest = args.partition(" ")
    sub = sub.lower()
    if sub in {"on", "开", "启用"}:
        await ctx.settings.set("welcome_enabled", True)
        await ctx.reply("已开启入群欢迎")
    elif sub in {"off", "关", "停用"}:
        await ctx.settings.set("welcome_enabled", False)
        await ctx.reply("已关闭入群欢迎")
    elif sub in {"set", "设置"}:
        template = rest.strip()
        if not template:
            await ctx.reply("用法：/welcome set 欢迎 {nickname} 加入～")
            return
        await ctx.settings.set("welcome_template", template)
        await ctx.reply("欢迎语已更新")
    else:
        await ctx.reply("用法：/welcome on|off|set <模板>")


@plugin.on_command("badword", aliases=("违禁词",), permission="group_admin", priority=5)
async def badword_control(ctx: Context) -> None:
    args = ctx.args.strip()
    current = [str(item) for item in (ctx.settings.get("badwords") or [])]

    if not args or args.lower() in {"list", "列表"}:
        if not current:
            await ctx.reply("当前没有违禁词")
            return
        await ctx.reply("违禁词：\n" + "\n".join(f"- {item}" for item in current))
        return

    sub, _, rest = args.partition(" ")
    sub = sub.lower()
    value = rest.strip()
    if sub in {"add", "添加"}:
        if not value:
            await ctx.reply("用法：/badword add 词")
            return
        if value not in current:
            current.append(value)
            await ctx.settings.set("badwords", current)
        await ctx.reply(f"已添加违禁词：{value}（共 {len(current)} 个）")
    elif sub in {"del", "delete", "删除"}:
        if value not in current:
            await ctx.reply(f"没有这个违禁词：{value}")
            return
        current.remove(value)
        await ctx.settings.set("badwords", current)
        await ctx.reply(f"已删除违禁词：{value}（剩 {len(current)} 个）")
    elif sub in {"clear", "清空"}:
        await ctx.settings.set("badwords", [])
        await ctx.reply("已清空违禁词")
    else:
        await ctx.reply("用法：/badword add|del|list [词]")
