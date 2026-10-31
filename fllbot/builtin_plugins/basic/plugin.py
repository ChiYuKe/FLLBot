# Plugin: basic | Role: implementation | Metadata: plugin.toml
"""基础命令：连通性测试、复读与帮助。"""

from __future__ import annotations

from fllbot.plugin import Context, Plugin

plugin = Plugin.from_manifest(__file__)


def _prefix(ctx: Context) -> str:
    for prefix in ctx.config.bot.command_prefix:
        if prefix:
            return prefix
    return ""


@plugin.on_command("ping", aliases=("在吗", "pong"))
async def ping(ctx: Context) -> None:
    await ctx.reply("pong", quote=True)


@plugin.on_command("echo", aliases=("复读", "repeat"))
async def echo(ctx: Context) -> None:
    text = ctx.args or ctx.readable
    if not text:
        await ctx.reply("用法：/echo 任意内容")
        return
    await ctx.reply(text)


@plugin.on_command("help", aliases=("帮助", "菜单", "commands"), priority=5)
async def show_help(ctx: Context) -> None:
    prefix = _prefix(ctx)
    grouped: dict[str, tuple[str, list[str]]] = {}

    for spec in ctx.bot.dispatcher.command_handlers():
        owner = ctx.bot.plugins.get(spec.plugin_name)
        if owner is None:
            continue
        names = [part for part in spec.usage.split("|") if part]
        if not names:
            continue
        main, *aliases = names
        line = f"{prefix}{main}"
        if aliases:
            line += "（别名 " + "、".join(f"{prefix}{alias}" for alias in aliases) + "）"
        entry = grouped.setdefault(owner.name, (owner.description or owner.name, []))
        entry[1].append(line)

    lines = ["可用命令："]
    for name in sorted(grouped):
        description, items = grouped[name]
        lines.append(f"\n[{name}] {description}")
        lines.extend(f"  {item}" for item in items)
    if not grouped:
        lines.append("（当前没有命令插件）")
    await ctx.reply("\n".join(lines))


@plugin.on_command("plugins", aliases=("插件",), priority=5)
async def list_plugins(ctx: Context) -> None:
    entries = ctx.bot.plugin_summaries()
    if not entries:
        await ctx.reply("当前没有已加载的插件")
        return
    lines = ["已加载插件："]
    for item in sorted(entries, key=lambda entry: entry["name"]):
        flag = "启用" if item.get("enabled") else "停用"
        suffix = "（内置）" if item.get("builtin") else ""
        lines.append(f"- {item['name']} v{item['version']} [{flag}]{suffix}：{item['description'] or '无说明'}")
    await ctx.reply("\n".join(lines))


@plugin.on_ai_tool(
    "check_online",
    title="运行状态",
    description="检查 FLLBot 当前是否正在运行。",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)
async def check_online(ctx: Context) -> dict[str, bool]:
    return {"online": bool(ctx.bot.started)}
