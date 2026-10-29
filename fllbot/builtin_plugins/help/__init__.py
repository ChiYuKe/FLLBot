"""帮助与插件列表。"""

from __future__ import annotations

from fllbot.plugin import Context, Plugin

plugin = Plugin(name="help", version="0.1.0", description="列出可用命令与插件状态")


def _prefix(ctx: Context) -> str:
    for prefix in ctx.config.bot.command_prefix:
        if prefix:
            return prefix
    return ""


@plugin.on_command("help", aliases=("帮助", "菜单", "commands"), priority=5)
async def show_help(ctx: Context) -> None:
    prefix = _prefix(ctx)
    grouped: dict[str, tuple[str, str, list[str]]] = {}

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
        entry = grouped.setdefault(owner.name, (owner.description or owner.name, "", []))
        entry[2].append(line)

    lines = ["可用命令："]
    for name in sorted(grouped):
        description, _, items = grouped[name]
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
