# Plugin: emotion | Role: declarations and lifecycle | Metadata: plugin.toml
"""群聊情绪与主动悄悄话。

- 按群记录机器人的心情和精神，随聊天内容与时间缓慢变化；
- 给智能体一个实时心情工具和一段语气提示，让回复不那么像客服；
- 在群友正聊天时，按冷却和额度给其中一人发一条群临时会话（私聊），
  内容可能是牢骚或悄悄话；模型不可用时使用本地句子。
"""

from __future__ import annotations

import random
from typing import Any

from fllbot.plugin import Context, Plugin

from .engine import EmotionEngine
from .store import MoodStore


class EmotionPlugin(Plugin):
    engine: EmotionEngine | None = None

    async def on_load(self, bot: Any) -> None:
        store = MoodStore(self.data_dir / "emotion.db")
        try:
            self.engine = EmotionEngine(self, store)
        except Exception:
            store.close()
            raise
        self.logger.info("情绪插件已加载：%d 个群的情绪记录", self.engine.mood_count)

    async def on_unload(self, bot: Any) -> None:
        engine, self.engine = self.engine, None
        if engine is not None:
            await engine.close()


plugin = EmotionPlugin.from_manifest(__file__)

plugin.setting(
    "enabled",
    type="bool",
    label="启用情绪与悄悄话",
    default=True,
    group_scoped=True,
    description="关闭后本群不再记录情绪，也不会发送悄悄话。",
)
plugin.setting(
    "whisper_enabled",
    type="bool",
    label="允许主动私聊",
    default=True,
    group_scoped=True,
    description="关闭后只记录情绪，不向群友发送临时会话；/情绪 仍可查看心情。",
)
plugin.setting(
    "style",
    type="select",
    label="悄悄话类型",
    default="mix",
    options=("mix", "whine", "secret"),
    group_scoped=True,
    description="mix 按心情混合；whine 偏牢骚；secret 偏悄悄话。",
)
plugin.setting(
    "probability",
    type="int",
    label="每轮尝试概率（%）",
    default=35,
    min=0,
    max=100,
    group_scoped=True,
    description="每个满足间隔和额度的群，每次轮询尝试发送的概率；0 表示不主动发送。",
)
plugin.setting(
    "min_interval_minutes",
    type="int",
    label="同群最小间隔（分钟）",
    default=45,
    min=1,
    max=720,
    group_scoped=True,
    description="同一个群两次悄悄话之间的最小间隔。",
)
plugin.setting(
    "active_window_minutes",
    type="int",
    label="活跃时间窗（分钟）",
    default=20,
    min=2,
    max=120,
    description="只挑这个时间窗内在群里发过言的群友。",
)
plugin.setting(
    "min_messages",
    type="int",
    label="最少群消息数",
    default=3,
    min=1,
    max=50,
    description="时间窗内至少有多少条消息才考虑发悄悄话，避免死群被点名。",
)
plugin.setting(
    "per_user_cooldown_hours",
    type="int",
    label="同一用户冷却（小时）",
    default=24,
    min=1,
    max=168,
    description="同一个群友两次收到悄悄话之间的最小间隔，跨群共用。",
)
plugin.setting(
    "daily_limit",
    type="int",
    label="每群每天上限",
    default=3,
    min=0,
    max=20,
    description="每个群每天最多发送条数；0 表示不限制。",
)
plugin.setting(
    "global_daily_limit",
    type="int",
    label="单账号每天上限",
    default=8,
    min=0,
    max=50,
    description="同一个机器人账号在全部群每天最多发送条数；0 表示不限制。",
)
plugin.setting(
    "quiet_hours",
    type="string",
    label="安静时段",
    default="00:30-07:00",
    description="例如 00:30-07:00，按北京时间；跨午夜也可，留空表示不限制。主动悄悄话不会在这个时段发送。",
)
plugin.setting(
    "max_chars",
    type="int",
    label="悄悄话最长字数",
    default=60,
    min=16,
    max=200,
    description="生成或本地句子会被裁剪到这个长度以内。",
)
plugin.setting(
    "llm_enabled",
    type="bool",
    label="使用模型生成",
    default=True,
    description="关闭或模型不可用时，改用内置的情绪化短句。",
)
plugin.setting(
    "include_context",
    type="bool",
    label="参考最近群聊",
    default=True,
    description="生成时把最近几条群聊作为背景交给模型；关闭可减少上下文外发。",
)
plugin.setting(
    "archive_history",
    type="bool",
    label="写入私聊历史",
    default=True,
    description="把发出的悄悄话写入私聊历史，群友私聊回复时机器人能接上前文。",
)
plugin.setting(
    "delivery",
    type="select",
    label="发送方式",
    default="temp",
    options=("temp", "private"),
    description=(
        "temp 使用群临时会话（send_private_msg 带 group_id，需要 NapCat/QQ 允许临时会话）；"
        "private 走普通好友私聊，只对已成为好友的群友有效。"
    ),
)
plugin.setting(
    "persona",
    type="string",
    label="生成用人格",
    default="",
    description="生成悄悄话时使用的人格名；留空沿用本群或默认人格。",
)
plugin.setting(
    "mood_decay_minutes",
    type="int",
    label="情绪回落半衰期（分钟）",
    default=45,
    min=5,
    max=720,
    description="心情和精神以该半衰期缓慢回到基线，避免一次夸奖或一句气话影响太久。",
)
plugin.setting(
    "only_interacted",
    type="bool",
    label="只找和机器人说过话的人",
    default=True,
    description=(
        "打开后只挑最近和机器人说过话（@、唤醒词、命令或私聊）的群友；"
        "关闭后按任意活跃群友选择。"
    ),
)
plugin.setting(
    "interaction_window_days",
    type="int",
    label="互动有效期（天）",
    default=30,
    min=1,
    max=90,
    description="群友在多少天内和机器人说过话才算符合条件；私聊互动对所有群生效。",
)

plugin.agent_instruction(
    "emotion_style",
    tools=["read_mood"],
    text=(
        "你是有情绪、有脾气的群友，不是客服或说明书。"
        "回复可以自然带一点个人态度：开心时话多一点、会接梗；烦躁时短一点、有点嘴硬；"
        "困的时候可以慢半拍。不要用“首先/其次/总之”的模板腔，也不要反复声明自己是 AI。"
        "需要知道自己此刻的心情、或想顺着情绪说话时，先调用 emotion__read_mood 看看；"
        "情绪只是内部状态，不要把数值念给群友，也不要在每条回复里汇报心情。"
    ),
)
plugin.agent_instruction(
    "emotion_whisper_style",
    tools=["send_whisper"],
    text=(
        "使用 emotion__send_whisper 前先确认群友确实在要求私聊、偷偷说或单独告诉你；"
        "一次只发一条，不要在群里宣布你发了私聊，也不要替别人转达。"
    ),
)


@plugin.on_message(priority=5, name="observe_group_chat")
async def observe_group_chat(ctx: Context) -> None:
    engine = plugin.engine
    if engine is None or not (ctx.is_group or ctx.is_private):
        return
    try:
        if not bool(ctx.settings.get("enabled", True)):
            return
        engine.observe(ctx)
    except Exception:
        plugin.logger.debug("记录群聊情绪失败", exc_info=True)


@plugin.on_notice("notify", priority=20)
async def observe_poke(ctx: Context) -> None:
    event = ctx.event
    if not getattr(event, "is_poke", False) or getattr(event, "sub_type", "") != "poke":
        return
    if ctx.group_id is None or int(getattr(event, "target_id", 0) or 0) != ctx.self_id:
        return
    engine = plugin.engine
    if engine is None:
        return
    try:
        if not bool(ctx.settings.get("enabled", True)):
            return
        engine.observe_poke(ctx)
    except Exception:
        plugin.logger.debug("记录戳一戳情绪失败", exc_info=True)


@plugin.on_loop(60, initial_delay=90, name="whisper_tick")
async def whisper_tick(bot: Any) -> None:
    engine = plugin.engine
    if engine is None:
        return
    try:
        await engine.tick(bot)
    except Exception:
        plugin.logger.debug("悄悄话轮询失败", exc_info=True)


@plugin.on_command("情绪", aliases=("心情", "mood"), priority=8, block=True)
async def mood_command(ctx: Context) -> None:
    if not ctx.is_group:
        await ctx.reply("情绪是按群记录的，去群里发 /情绪 看吧。")
        return
    engine = plugin.engine
    if engine is None:
        return
    if ctx.argv and ctx.argv[0].lower() in {"重置", "reset", "恢复"}:
        if not ctx.is_superuser:
            await ctx.reply("只有超级管理员能重置情绪。")
            return
        engine.reset(ctx.self_id, ctx.group_id)
        await ctx.reply("好，这个群的情绪已经重置。")
        return
    await ctx.reply(engine.status_text(ctx))


@plugin.on_command("悄悄话", aliases=("私聊", "whisper"), priority=9, block=True)
async def whisper_command(ctx: Context) -> None:
    engine = plugin.engine
    if engine is None:
        return
    action = ctx.args.strip().lower()
    if not ctx.is_group:
        if action in {"关闭", "关", "停", "别发", "别私聊", "不要"}:
            engine.opt_out(ctx.self_id, ctx.user_id)
            await ctx.reply("好，以后不再主动私聊你了。")
        elif action in {"开启", "开", "恢复", "打开"}:
            engine.opt_in(ctx.self_id, ctx.user_id)
            await ctx.reply("好，之后又可能收到我的悄悄话了。")
        else:
            await ctx.reply("在群里发 /悄悄话 可以查看状态；发 /悄悄话 关闭 可以不再收私聊。")
        return
    if action in {"关闭", "关", "停", "别发", "别私聊", "不要"}:
        engine.opt_out(ctx.self_id, ctx.user_id)
        await ctx.reply("好，以后不在群里私聊你了。")
        return
    if action in {"开启", "开", "恢复", "打开"}:
        engine.opt_in(ctx.self_id, ctx.user_id)
        await ctx.reply("好，之后又可能收到我的悄悄话了。")
        return
    await ctx.reply(engine.whisper_text(ctx))


@plugin.on_command("哄哄", aliases=("夸夸", "摸摸"), priority=9, block=True, cooldown=60)
async def praise_command(ctx: Context) -> None:
    if not ctx.is_group:
        await ctx.reply("在群里哄我才有用。")
        return
    engine = plugin.engine
    if engine is None:
        return
    if not bool(ctx.settings.get("enabled", True)):
        await ctx.reply("本群的情绪插件已关闭。")
        return
    mood = engine.praise(ctx)
    if mood.valence >= 0.55:
        await ctx.reply(random.choice(("嘿嘿，今天心情好，你说什么都好听。", "被夸到了，尾巴要翘起来了。")))
    elif mood.valence >= 0.25:
        await ctx.reply(random.choice(("嗯……被你哄到了，说话都软了点。", "行吧行吧，这次算你有心。")))
    else:
        await ctx.reply(random.choice(("嘴硬一下：我才没有很开心。", "哼，别以为这样我就会原谅你们。")))


@plugin.on_ai_tool(
    "read_mood",
    effect="read_only",
    title="查看当前心情",
    description=(
        "查看机器人在当前群的情绪状态：心情、精神、最近原因，以及悄悄话额度。"
        "想顺着情绪调整语气时先调用；这是内部状态，不要把数值原样念给群友。"
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)
async def read_mood(ctx: Context) -> dict[str, Any]:
    engine = plugin.engine
    if engine is None:
        return {"error": "unavailable", "message": "情绪记录尚未就绪。"}
    return engine.mood_payload(ctx)


@plugin.on_ai_tool(
    "send_whisper",
    effect="side_effecting",
    capabilities=("qq.send",),
    permission="group",
    title="发一条悄悄话",
    description=(
        "给当前发言人发一条只有 TA 能看到的临时会话（私聊）。"
        "仅当群友明确要求“私聊我”“偷偷说”“发句悄悄话”时使用；不能指定别人，不能连续发送，"
        "自动参与时禁止。kind 可选 whine（牢骚）或 secret（悄悄话），省略则按当前心情选择。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["whine", "secret"],
                "description": "whine 为牢骚，secret 为悄悄话；省略按心情决定",
            },
        },
        "additionalProperties": False,
    },
    timeout=60,
    max_output_chars=1000,
)
async def send_whisper(ctx: Context, kind: str = "") -> dict[str, Any]:
    engine = plugin.engine
    if engine is None:
        return {"error": "unavailable", "message": "情绪记录尚未就绪。"}
    if not ctx.is_group or ctx.group_id is None:
        return {"error": "group_only", "message": "只能在群里发临时会话。"}
    decision = getattr(ctx, "trigger_decision", None)
    if decision is not None and not getattr(decision, "addressed", True):
        return {"error": "addressed_required", "message": "只有群友明确要求时才能发悄悄话。"}
    key = ("emotion", f"tool:{ctx.self_id}:{ctx.group_id}", ctx.user_id)
    if not ctx.bot.permissions.cooldown_ok(key, 600):
        return {"error": "cooldown", "message": "刚发过悄悄话，过一会儿再说。"}
    result = await engine.whisper_to(
        bot=ctx.bot,
        self_id=ctx.self_id,
        group_id=ctx.group_id,
        user_id=ctx.user_id,
        nickname=ctx.nickname,
        kind=kind or None,
    )
    if result.get("error"):
        result.setdefault("sent", False)
    return result
