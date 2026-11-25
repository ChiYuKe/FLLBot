"""Choose how a final chat reply is delivered with the model's help."""

import re

from fllbot.plugin import Context, Plugin, requests_text_only
from fllbot.plugin.rendering import MAX_CHARS, markdown_table_spans

from .workflows import workflow_instructions

_FENCED_BLOCK = re.compile(r"^[ \t]*(?:`{3,}|~{3,})", re.MULTILINE)
_FORWARD_REQUEST = re.compile(
    r"(?:用|发|合并成|转发成|转成|做成|整理成|只要|发成)\s*(?:一条)?(?:QQ\s*)?(?:合并)?聊天记录",
    re.I,
)


class ChatDispatch:
    def can_choose(self, mode: str, text: str | None) -> bool:
        return mode in {"auto", "auto_image"} or (mode != "split" and requests_text_only(text))

    def instructions(self, chunk_size: int) -> str:
        length = f"普通消息每条约 {chunk_size} 字符。" if chunk_size > 0 else ""
        return (
            "\n\n需要直接回答文字时，自行选择本轮正文的 QQ 发送方式。"
            f"{length}简短且能放进一条普通消息的答复用 <REPLY_TEXT>。"
            "长说明、完整代码、多段提示词和预计会拆成多条的正文优先用 <REPLY_FORWARD>。"
            "用户明确指定用聊天记录时也使用 <REPLY_FORWARD>。"
            "适合逐列对比时，在正文中写标准 Markdown 表格。合并聊天记录可同时包含表格图片和正文文字。"
            "最终回复首行单独写一个发送标记，下一行开始写正文。正文结束后直接结束。"
            "工具调用和静默标记无需添加发送标记。"
        )

    def tool_instructions(self, tool_names: set[str], *, text_only: bool = False) -> str:
        if not tool_names:
            return ""
        rules = (
            "\n\n聊天调度：先识别本轮目标和用户指定的格式。只从本轮实际提供的工具选最贴合任务的工具，"
            "多步任务按依赖顺序执行。先取得事实或产物，再调用交付工具。"
            "复合请求要覆盖所有子任务；只在成品能覆盖整项请求时，将直接发送工具作为唯一调用。"
            "仅在用户本轮明确要求写入、修改、发布、外发或管理服务时执行对应操作。"
            "被拒绝时只考虑同样符合权限的替代方案；发出结果不确定时不要换渠道重发。"
            "交付确认成功后只补充没有交付的信息。"
        )
        if text_only:
            rules += "本轮交付正文用文字。需要事实时仍先使用已授权的读取工具。"
        else:
            rules += (
                "用户未指定格式时，优先选择能完整回答问题的专用卡片；代码、需复制内容或短说明优先用文字。"
            )
        if not text_only and "web__weather_card" in tool_names:
            rules += (
                "例如「明天武汉天气如何」属于标准天气预报，直接调用 web__weather_card，"
                "将武汉传为 city、days 至少填 2，确保预报覆盖明天。直接使用这个工具查询和交付。"
            )
        if "web__get_weather" in tool_names:
            rules += "要求文字、卡片不可用或明确未发出且缺数据时，用 web__get_weather 查询，按具体日期回答。"
        if not text_only and "github__render_card" in tool_names:
            rules += (
                "GitHub 概览、贡献者或 Issue/PR 列表适合直接用 github__render_card 查询并交付；"
                "深入分析、源码或需要复制时使用可用的仓库读取工具。"
            )
        return rules + workflow_instructions(tool_names)

    def deduplicate_delivery(self, capabilities: tuple[str, ...]) -> bool:
        return any(cap in {"qq.send", "qq.send_file", "email.send"} for cap in capabilities)

    def should_forward(self, text: str, part_count: int) -> bool:
        return part_count >= 3 or (part_count == 2 and bool(_FENCED_BLOCK.search(text)))

    def markdown_tables(self, text: str) -> list[tuple[int, int]]:
        return markdown_table_spans(text) if len(text) <= MAX_CHARS else []

    def requests_forward(self, user_text: str) -> bool:
        return bool(_FORWARD_REQUEST.search(user_text))


plugin = Plugin.from_manifest(__file__)
plugin.chat_dispatch = ChatDispatch()

plugin.setting(
    "auto_approve_friend_requests",
    type="bool",
    label="自动同意好友申请",
    default=False,
    description="对本实例接入的启用账号生效。收到 QQ 好友申请时直接同意，无需群聊消息或模型判断。",
)


@plugin.on_request("friend")
async def auto_approve_friend_request(ctx: Context) -> None:
    if ctx.settings.get("auto_approve_friend_requests"):
        await ctx.approve_friend_request()


plugin.agent_instruction(
    "friend_requests",
    tools=["request_friendship"],
    text=(
        "仅在本轮群消息的发送者明确要求你添加本人为 QQ 好友时，调用 "
        "chat_dispatch__request_friendship，例如「@Bot 加我好友」「可以加一下我的 QQ 吗」。"
        "检查本轮发言者自己的文字。聊到好友、询问怎么加、替别人申请、转述或引用旧申请时保持普通回复。"
        "每轮最多调用一次。根据工具返回的 message 回复；提交好友申请后提醒对方在 QQ 查看。"
        "只有工具确认提交后才说已发送申请。只有对方接受后才会成为好友。"
    ),
)


@plugin.on_ai_tool(
    "request_friendship",
    title="向当前发言者申请 QQ 好友",
    description="当前群消息发送者明确请求你加本人为好友时使用。请求目标取自 QQ 当前事件。",
    permission="group",
    capabilities=("qq.send",),
    timeout=16.0,
)
async def request_friendship(ctx: Context) -> dict:
    return await ctx.request_friendship()
