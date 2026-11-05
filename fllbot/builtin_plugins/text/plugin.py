"""Plugin: text | Role: agent-only tool registration | Metadata: plugin.toml."""

from __future__ import annotations

import asyncio
import base64

from fllbot.llm.output import send_media_reply
from fllbot.onebot.segments import MessageSegment
from fllbot.plugin import Context, Plugin
from fllbot.rendering.text_image import join_pages as _join_pages

from .service import MAX_CHARS, process, render

plugin = Plugin.from_manifest(__file__)
plugin.agent_instruction(
    'image_delivery', tools=['render_image'],
    text='用户要求图片时，先完成并整理答复，再对完整内容调用 render_image 一次；'
         '不要把草稿或逐段内容分次渲染。图片发送后不再重复图片中的长文。'
         '摘要默认控制在一页，用户明确要求详细分析时保留必要章节和细节。',
)
plugin.setting(
    "font_path",
    type="string",
    label="中文字体路径",
    default="",
    description="留空自动查找系统中文字体；也可填写 TTF/OTF/TTC 文件的绝对路径。",
)
plugin.setting("font_size", type="int", label="图片字号", default=24, min=16, max=48)
plugin.setting("image_width", type="int", label="图片宽度", default=900, min=400, max=1600)


def schema(**properties: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_CHARS,
                "description": f"要处理的完整文本，最多 {MAX_CHARS} 字符",
            },
            **properties,
        },
        "required": ["text", *properties],
        "additionalProperties": False,
    }


def modes(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


async def run(operation: str, text: str, **options: str) -> dict:
    try:
        return await asyncio.to_thread(process, operation, text, **options)
    except ValueError as exc:
        return {"error": str(exc)}


@plugin.on_ai_tool(
    "statistics",
    effect="read_only",
    title="文本统计",
    description="统计字符数、非空白字符数、行数、汉字数与英文单词数。",
    parameters=schema(),
)
async def statistics(ctx: Context, text: str) -> dict:
    return await run("statistics", text)


@plugin.on_ai_tool(
    "transform",
    effect="read_only",
    title="文本整理",
    description="按行去除首尾空白、删除空行、精确去重（保留首次出现顺序）、排序、大小写或 NFKC 全半角规范化。",
    parameters=schema(
        mode=modes("trim", "remove_blank_lines", "deduplicate_lines", "sort_lines", "lower", "upper", "normalize")
    ),
    max_output_chars=24000,
)
async def transform(ctx: Context, text: str, mode: str) -> dict:
    return await run("transform", text, mode=mode)


@plugin.on_ai_tool(
    "replace",
    effect="read_only",
    title="文本替换",
    description="按字面值替换全部匹配文本并返回替换次数；不支持正则表达式。",
    parameters=schema(
        old={"type": "string", "minLength": 1, "maxLength": MAX_CHARS}, new={"type": "string", "maxLength": MAX_CHARS}
    ),
    max_output_chars=24000,
)
async def replace(ctx: Context, text: str, old: str, new: str) -> dict:
    return await run("replace", text, old=old, new=new)


@plugin.on_ai_tool(
    "format_json",
    effect="read_only",
    title="JSON 格式化",
    description="校验 JSON 并美化缩进（pretty）或压缩（compact），保留中文。",
    parameters=schema(mode=modes("pretty", "compact")),
    max_output_chars=24000,
)
async def format_json(ctx: Context, text: str, mode: str) -> dict:
    return await run("json", text, mode=mode)


@plugin.on_ai_tool(
    "codec",
    effect="read_only",
    title="文本编解码",
    description="UTF-8 文本的 Base64、URL 百分号编解码及 HTML 实体转义/反转义；不处理二进制文件。",
    parameters=schema(
        mode=modes("base64_encode", "base64_decode", "url_encode", "url_decode", "html_escape", "html_unescape")
    ),
    max_output_chars=24000,
)
async def codec(ctx: Context, text: str, mode: str) -> dict:
    return await run("codec", text, mode=mode)


image_parameters = schema(format=modes("markdown", "plain"))
image_parameters["required"] = ["text"]
image_parameters["properties"]["format"]["default"] = "markdown"


@plugin.on_ai_tool(
    "render_image",
    effect="side_effecting",
    title="文本转图片",
    timeout=60,
    description="将文本排版为 PNG 并发送到当前会话；自动换行分页（最多20页）。"
    "是否调用由你结合用户意图和内容判断，不要因为文本含有 Markdown 符号就自动转图。"
    "先在内部完成并整理完整答复，再对整段内容调用本工具一次；绝不要按段落/章节分次渲染，也不要把草稿、引用的原问题或代码片段单独做图。"
    "工具发送图片后，最终聊天回复只留一句简短提示或补充，不要再复制一遍图片中的长文。"
    "通常短回答、简单列表、代码片段、用户只是提问/要求解释或总结时直接文字回复。"
    "当答复较长且层次明显，排版图片明显更易读时，可将整理后的答复排版成图片发送。"
    "摘要或介绍默认先提炼重点并控制在一页；不要原样搬入整篇 README、长聊天记录或所有目录条目。"
    "只有用户明确要求全文/详细版本时才生成多页，尽量不超过 2 页；更多内容先压缩或询问是否需要完整长图。"
    "用户明确要求图片时优先按要求发送；用户只要求简短提炼时不要把长篇原文改成图片。"
    "默认按 Markdown 排版，支持标题、加粗、列表、引用、代码块、表格及链接文字；"
    "长篇技术分析或 Markdown 文档允许生成一张连续长图，最多 20 个排版页；保持章节、代码和解释完整，不擅自压缩成一页摘要。"
    "需逐字显示 Markdown 符号时选择 format=plain。HTML 按原文显示，图片只显示说明，不加载外部资源。",
    parameters=image_parameters,
)
async def render_image(ctx: Context, text: str, format: str = "markdown") -> dict:
    settings = ctx.settings
    try:
        pages = await asyncio.to_thread(
            render,
            text,
            font_path=str(settings.get("font_path") or ""),
            font_size=int(settings.get("font_size", 24)),
            width=int(settings.get("image_width", 900)),
            format=format,
        )
    except (ValueError, ImportError) as exc:
        return {"error": "缺少图片渲染依赖，请更新安装 FLLBot" if isinstance(exc, ImportError) else str(exc)}
    # Keep long documents as one QQ image/message instead of flooding the chat
    # with a separate image segment for each rendered page.
    try:
        image_data = await asyncio.to_thread(_join_pages, pages)
    except (ValueError, OSError) as exc:
        return {"error": f"无法合成长图：{exc}"}
    segments = [MessageSegment.image("base64://" + base64.b64encode(image_data).decode("ascii"))]
    sent = await send_media_reply(ctx, segments, text, require_ai=True)
    return {"sent": sent, "images": 1 if sent else 0, "pages_in_image": len(pages) if sent else 0}
