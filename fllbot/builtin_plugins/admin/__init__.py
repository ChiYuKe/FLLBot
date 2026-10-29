"""运行状态与热操作（超管）：/status、/reload、/plugin、/group。"""

from __future__ import annotations

import time

from fllbot import __version__
from fllbot.plugin import Context, Plugin

plugin = Plugin(name="admin", version="0.2.0", description="运行状态与热操作（超管）")


def _humanize(seconds: float) -> str:
    total = int(max(0, seconds))
    if total >= 86400:
        return f"{total // 86400} 天 {total % 86400 // 3600} 小时"
    if total >= 3600:
        return f"{total // 3600} 小时 {total % 3600 // 60} 分"
    if total >= 60:
        return f"{total // 60} 分 {total % 60} 秒"
    return f"{total} 秒"


@plugin.on_command("status", aliases=("运行状态", "状态"), permission="superuser", priority=5)
async def show_status(ctx: Context) -> None:
    bot = ctx.bot
    connections = bot.manager.summaries()
    plugins = bot.plugin_summaries()
    enabled = sum(1 for item in plugins if item.get("enabled"))
    uptime = (time.time() - bot.started_at) if bot.started_at else 0
    llm = bot.chat.status()

    lines = [
        f"FLLBot {__version__}（已运行 {_humanize(uptime)}）",
        f"连接：{len(connections)} 个"
        + ("：" + "、".join(str(item.get('self_id') or '?') for item in connections) if connections else "（无）"),
        f"插件：{len(plugins)} 个，启用 {enabled} 个",
        f"LLM：{'可用' if llm.get('available') else '未启用'}"
        + (f"（{llm.get('model')}）" if llm.get("available") else ""),
        f"消息总数：{await bot.storage.total_messages()}",
    ]
    stats = bot.dispatcher.summary().get("stats") or {}
    if stats:
        lines.append("分发：" + "、".join(f"{key}={value}" for key, value in sorted(stats.items())))
    await ctx.reply("\n".join(lines))


@plugin.on_command("reload", aliases=("重载",), permission="superuser", priority=5)
async def reload_plugin(ctx: Context) -> None:
    name = ctx.args.strip()
    if not name:
        await ctx.reply("用法：/reload <插件名>")
        return
    if ctx.bot.plugins.get(name) is None:
        await ctx.reply(f"插件不存在或未加载：{name}")
        return
    try:
        await ctx.bot.plugins.reload(name)
    except Exception as exc:
        await ctx.reply(f"重载失败：{exc}")
        return
    await ctx.reply(f"已重载插件 {name}")


@plugin.on_command("plugin", aliases=("插件开关",), permission="superuser", priority=5)
async def toggle_plugin(ctx: Context) -> None:
    parts = ctx.args.split()
    if len(parts) < 2:
        await ctx.reply("用法：/plugin on|off <插件名>")
        return
    action, name = parts[0].lower(), parts[1]
    if action not in {"on", "off", "enable", "disable", "开", "关"}:
        await ctx.reply("用法：/plugin on|off <插件名>")
        return
    if ctx.bot.plugins.get(name) is None:
        await ctx.reply(f"插件不存在：{name}")
        return
    enabled = action in {"on", "enable", "开"}
    await ctx.bot.permissions.set_plugin_enabled(name, enabled)
    await ctx.reply(f"插件 {name} 已{'启用' if enabled else '停用'}")


@plugin.on_command("group", aliases=("群开关",), permission="group_admin", priority=5)
async def toggle_group(ctx: Context) -> None:
    if not ctx.is_group or not ctx.group_id:
        await ctx.reply("这个命令只能在群里使用")
        return
    action = ctx.args.strip().lower()
    if action in {"on", "开", "启用"}:
        await ctx.bot.permissions.set_group_enabled(ctx.group_id, True)
        await ctx.reply("本群已启用机器人")
    elif action in {"off", "关", "停用"}:
        await ctx.bot.permissions.set_group_enabled(ctx.group_id, False)
        await ctx.reply("本群已停用机器人，重新开启请私聊超管或用管理 API")
    else:
        await ctx.reply("用法：/group on|off")
