"""好友 / 入群请求提醒与审批。

NapCat 收到的好友申请、加群申请会推给超管私聊，超管可用
``/approve <标记>`` 或 ``/reject <标记> [理由]`` 处理。
"""

from __future__ import annotations

from fllbot.onebot.models import RequestEvent
from fllbot.plugin import Context, Plugin

plugin = Plugin(
    name="request_notify",
    version="0.1.0",
    description="好友/入群请求提醒，超管可用 /approve、/reject 处理",
)

SCOPE = "pending_requests"


@plugin.on_request(priority=10)
async def notify_request(ctx: Context) -> None:
    event = ctx.event
    if not isinstance(event, RequestEvent) or not event.flag:
        return

    record = {
        "flag": event.flag,
        "request_type": event.request_type,
        "sub_type": event.sub_type,
        "group_id": event.group_id,
        "user_id": event.user_id,
        "comment": event.comment,
        "time": event.time,
    }
    await ctx.bot.storage.kv_set(SCOPE, event.flag, record)

    kind = "好友申请" if event.request_type == "friend" else "加群/邀请入群请求"
    lines = [f"收到新的{kind}"]
    if event.group_id:
        lines.append(f"群号：{event.group_id}")
    lines.append(f"用户：{event.user_id}")
    lines.append(f"附言：{event.comment or '（无）'}")
    lines.append(f"标记：{event.flag}")
    lines.append(f"处理：/approve {event.flag} 或 /reject {event.flag} [理由]")
    text = "\n".join(lines)

    for user_id in ctx.config.bot.superusers:
        try:
            await ctx.connection.send_private_msg(int(user_id), text)
        except Exception:
            ctx.logger.warning("通知超管 %s 失败", user_id, exc_info=True)

    ctx.stop()


@plugin.on_command("requests", aliases=("请求",), permission="superuser", priority=5)
async def list_requests(ctx: Context) -> None:
    items = await ctx.bot.storage.kv_all(SCOPE)
    if not items:
        await ctx.reply("当前没有待处理请求")
        return
    lines = ["待处理请求："]
    for item in items.values():
        lines.append(
            f"- {item.get('flag')} | {item.get('request_type')} | 用户 {item.get('user_id')} | {item.get('comment') or '无附言'}"
        )
    await ctx.reply("\n".join(lines))


@plugin.on_command("approve", aliases=("同意", "通过"), permission="superuser", priority=5)
async def approve(ctx: Context) -> None:
    flag = ctx.args.strip()
    if not flag:
        await ctx.reply("用法：/approve <标记>")
        return
    record = await ctx.bot.storage.kv_get(SCOPE, flag)
    if not isinstance(record, dict):
        await ctx.reply("没有找到该请求（可能已处理）")
        return
    try:
        if record.get("request_type") == "friend":
            await ctx.connection.set_friend_add_request(flag, True)
        else:
            await ctx.connection.set_group_add_request(flag, str(record.get("sub_type") or "add"), True, "")
    except Exception as exc:
        await ctx.reply(f"处理失败：{exc}")
        return
    await ctx.bot.storage.kv_delete(SCOPE, flag)
    await ctx.reply(f"已同意 {record.get('request_type')} / {record.get('user_id')}")


@plugin.on_command("reject", aliases=("拒绝",), permission="superuser", priority=5)
async def reject(ctx: Context) -> None:
    parts = ctx.args.split(maxsplit=1)
    flag = parts[0].strip() if parts else ""
    reason = parts[1].strip() if len(parts) > 1 else ""
    if not flag:
        await ctx.reply("用法：/reject <标记> [理由]")
        return
    record = await ctx.bot.storage.kv_get(SCOPE, flag)
    if not isinstance(record, dict):
        await ctx.reply("没有找到该请求（可能已处理）")
        return
    try:
        if record.get("request_type") == "friend":
            await ctx.connection.set_friend_add_request(flag, False)
        else:
            await ctx.connection.set_group_add_request(flag, str(record.get("sub_type") or "add"), False, reason)
    except Exception as exc:
        await ctx.reply(f"处理失败：{exc}")
        return
    await ctx.bot.storage.kv_delete(SCOPE, flag)
    await ctx.reply("已拒绝该请求")
