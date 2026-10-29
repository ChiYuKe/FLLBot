"""大模型对话插件：群聊 @ 机器人或私聊直接说话即可多轮对话。"""

from __future__ import annotations

from typing import Any

from fllbot.onebot.models import MessageEvent
from fllbot.plugin import Context, Plugin

plugin = Plugin(name="llm_chat", version="0.1.0", description="大模型多轮对话：群里 @ 我，或私聊直接说话")


def _llm_config(ctx: Context) -> Any:
    return ctx.config.llm


def _should_respond(ctx: Context) -> bool:
    config = _llm_config(ctx)
    if not config.enabled or not ctx.bot.chat.available:
        return False
    if not isinstance(ctx.event, MessageEvent):
        return False
    if not ctx.bot.chat.scene_enabled(ctx.event.scene, ctx.event.scene_id):
        return False
    if ctx.is_private:
        return True
    if config.trigger == "always":
        return True
    if config.trigger == "prefix":
        prefix = config.trigger_prefix or "ai"
        return ctx.text.startswith(prefix)
    return ctx.to_me


def _clean_prompt(ctx: Context) -> str:
    text = ctx.text
    config = _llm_config(ctx)
    if ctx.is_group and config.trigger == "prefix":
        prefix = config.trigger_prefix or "ai"
        if text.startswith(prefix):
            text = text[len(prefix) :].lstrip(" :：,，")
    return text.strip()


@plugin.on_message(rule=_should_respond, priority=90)
async def chat(ctx: Context) -> None:
    text = _clean_prompt(ctx)
    if not text:
        if ctx.to_me:
            await ctx.reply("在的，想聊点什么？", quote=True)
        return

    reply = await ctx.bot.chat.respond(ctx, text)
    if reply is None:
        return
    ctx.stop()


@plugin.on_command("chat", aliases=("对话",), priority=5, permission="superuser|group_admin")
async def chat_control(ctx: Context) -> None:
    scene = ctx.event.scene if isinstance(ctx.event, MessageEvent) else "private"
    scene_id = ctx.event.scene_id if isinstance(ctx.event, MessageEvent) else 0
    sub = ctx.argv[0].lower() if ctx.argv else ""

    if sub in {"clear", "reset", "重置", "清空"}:
        removed = await ctx.bot.chat.clear(scene, scene_id)
        await ctx.reply(f"已清空本会话上下文（{removed} 条）")
        return

    if sub in {"persona", "人格"}:
        if len(ctx.argv) >= 2:
            if not ctx.is_group or ctx.group_id is None:
                await ctx.reply("人格切换目前只支持在群里设置")
                return
            name = ctx.argv[1]
            persona = ctx.bot.personas.find(name)
            if persona is None:
                available = "、".join(ctx.bot.personas.names()) or "（无）"
                await ctx.reply(f"没有这个人格：{name}\n可用：{available}")
                return
            settings = ctx.bot.permissions.group_config(ctx.group_id)
            llm_settings = dict(settings.get("llm") or {})
            llm_settings["persona"] = name
            settings["llm"] = llm_settings
            await ctx.bot.permissions.set_group_config(ctx.group_id, settings)
            await ctx.reply(f"本群人格已切换为「{persona.title or name}」")
            return

        current = ctx.bot.chat.persona_name(scene, scene_id)
        available = "、".join(ctx.bot.personas.names()) or "（无）"
        await ctx.reply(f"当前人格：{current}\n可用人格：{available}")
        return

    await ctx.reply("用法：\n/chat clear —— 清空本会话上下文\n/chat persona —— 查看当前人格\n/chat persona <名称> —— 切换本群人格")
