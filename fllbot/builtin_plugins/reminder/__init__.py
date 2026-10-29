"""定时提醒：``/remind 10m 喝水``，到点由后台循环发回原会话。"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from fllbot.onebot.models import MessageEvent
from fllbot.plugin import Context, Plugin

plugin = Plugin(name="reminder", version="0.2.0", description="定时提醒：/remind 10m 喝水")

plugin.setting("max_per_scene", type="int", label="每个会话最多待提醒数", default=20, min=1, max=200)
plugin.setting("allow_private", type="bool", label="允许私聊提醒", default=True)
plugin.setting("text_template", type="string", label="提醒前缀", default="【提醒 #{id}】{content}")

logger = logging.getLogger("fllbot.plugin.reminder")

_DURATION_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(s|sec|秒|m|min|分|分钟|h|hour|小时|d|day|天|w|week|周)?$",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "秒": 1,
    "m": 60,
    "min": 60,
    "分": 60,
    "分钟": 60,
    "h": 3600,
    "hour": 3600,
    "小时": 3600,
    "d": 86400,
    "day": 86400,
    "天": 86400,
    "w": 604800,
    "week": 604800,
    "周": 604800,
}
_MIN_SECONDS = 10
_MAX_SECONDS = 30 * 86400
_LIST_LIMIT = 30


def parse_duration(text: str) -> float | None:
    """解析 ``30s`` / ``10m`` / ``2h`` / ``1d`` / ``5分钟``，无单位按分钟。"""
    match = _DURATION_RE.match((text or "").strip())
    if match is None:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "m").lower()
    seconds = amount * _UNIT_SECONDS.get(unit, 60)
    if seconds < _MIN_SECONDS or seconds > _MAX_SECONDS:
        return None
    return seconds


def humanize_seconds(seconds: float) -> str:
    total = int(seconds)
    if total >= 86400:
        return f"{total // 86400} 天"
    if total >= 3600:
        return f"{total // 3600} 小时"
    if total >= 60:
        return f"{total // 60} 分钟"
    return f"{total} 秒"


def _scene(ctx: Context) -> tuple[str, int]:
    event = ctx.event
    if isinstance(event, MessageEvent):
        return event.scene, event.scene_id
    return "private", ctx.user_id


async def _reply_list(ctx: Context) -> None:
    scene, scene_id = _scene(ctx)
    rows = await ctx.db.list_reminders(scene, scene_id)
    if not rows:
        await ctx.reply("当前没有待提醒的事项")
        return
    now = time.time()
    lines = ["待提醒："]
    for item in rows[:_LIST_LIMIT]:
        remaining = humanize_seconds(max(0, item["due_at"] - now))
        lines.append(f"- #{item['id']}（{remaining}后）：{item['content']}")
    if len(rows) > _LIST_LIMIT:
        lines.append(f"……共 {len(rows)} 条")
    await ctx.reply("\n".join(lines))


async def _delete(ctx: Context, raw_id: str) -> None:
    try:
        reminder_id = int(raw_id.strip().lstrip("#"))
    except ValueError:
        await ctx.reply("编号格式不对，例如：/remind del 3")
        return
    scene, scene_id = _scene(ctx)
    rows = await ctx.db.list_reminders(scene, scene_id)
    target = next((item for item in rows if item["id"] == reminder_id), None)
    if target is None:
        await ctx.reply(f"没有找到编号 #{reminder_id} 的提醒")
        return
    if target["creator"] != ctx.user_id and not ctx.is_group_admin:
        await ctx.reply("只有创建者或群管理员可以删除这条提醒")
        return
    if await ctx.db.delete_reminder(reminder_id, scene=scene, scene_id=scene_id):
        await ctx.reply(f"已删除提醒 #{reminder_id}")
    else:
        await ctx.reply("删除失败，可能已经触发过了")


@plugin.on_command("remind", aliases=("提醒",), priority=5)
async def create_reminder(ctx: Context) -> None:
    args = ctx.args.strip()
    if not args:
        await ctx.reply(
            "用法：\n"
            "/remind 10m 喝水 —— 10 分钟后提醒\n"
            "/remind 1h30m 开会 —— 暂不支持组合单位，请用 90m\n"
            "/remind list —— 查看待提醒\n"
            "/remind del 3 —— 删除编号 3"
        )
        return

    head, _, rest = args.partition(" ")
    head_lower = head.lower()
    if head_lower in {"list", "列表", "ls"}:
        await _reply_list(ctx)
        return
    if head_lower in {"del", "delete", "删除", "取消"}:
        await _delete(ctx, rest)
        return

    if not rest.strip():
        await ctx.reply("用法：/remind <时长> <内容>，例如 /remind 10m 喝水")
        return

    scene, scene_id = _scene(ctx)
    if scene == "private" and not ctx.settings.get("allow_private"):
        await ctx.reply("私聊提醒已关闭，请在群里使用")
        return

    seconds = parse_duration(head)
    if seconds is None:
        await ctx.reply("时长格式不对：支持 30s、10m、2h、1d（10 秒 ~ 30 天）")
        return

    content = rest.strip()
    if len(content) > 200:
        content = content[:200]

    limit = int(ctx.settings.get("max_per_scene") or 20)
    if await ctx.db.count_reminders(scene, scene_id) >= limit:
        await ctx.reply(f"待提醒太多了（上限 {limit} 条），先用 /remind list 清理一下吧")
        return

    due_at = time.time() + seconds
    reminder_id = await ctx.db.add_reminder(
        scene=scene,
        scene_id=scene_id,
        creator=ctx.user_id,
        content=content,
        due_at=due_at,
    )
    await ctx.reply(f"好，#{reminder_id}，{humanize_seconds(seconds)}后提醒：{content}")


@plugin.on_loop(15, name="reminder-tick", initial_delay=5)
async def tick(bot: Any) -> None:
    try:
        due = await bot.storage.due_reminders(time.time(), limit=20)
    except Exception:
        logger.exception("读取待提醒失败")
        return
    if not due:
        return

    connection = bot.manager.primary()
    if connection is None:
        logger.debug("当前没有在线连接，%d 条提醒等下次再发", len(due))
        return

    template = str(plugin.settings.get("text_template") or "【提醒 #{id}】{content}")
    for item in due:
        text = template.replace("{id}", str(item["id"])).replace("{content}", item["content"])
        try:
            if item["scene"] == "group":
                await connection.send_group_msg(int(item["scene_id"]), text)
            else:
                await connection.send_private_msg(int(item["scene_id"]), text)
        except Exception:
            logger.warning("提醒 #%s 发送失败，稍后重试", item["id"], exc_info=True)
            continue
        await bot.storage.mark_reminder_fired(int(item["id"]))
        logger.info("已发送提醒 #%s 到 %s:%s", item["id"], item["scene"], item["scene_id"])

    await bot.storage.purge_fired_reminders(7)
