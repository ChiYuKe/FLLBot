"""Plugin: text | Role: agent-only tool registration | Metadata: plugin.toml."""

from __future__ import annotations

import asyncio
import base64

from fllbot.llm.output import send_media_reply
from fllbot.onebot.segments import MessageSegment
from fllbot.plugin import Context, Plugin
from fllbot.rendering.diagram_render import render_diagram as render_diagram_image
from fllbot.rendering.text_image import join_pages as _join_pages

from .file_export import export_file
from .service import MAX_CHARS, process, render

plugin = Plugin.from_manifest(__file__)
plugin.agent_instruction(
    "file_delivery", tools=["send_html_file", "send_markdown_file"],
    text="用户要求 HTML 或 Markdown/MD 文件时，先整理完整内容，再调用对应文件工具一次发送附件。"
    "HTML 文件默认将 Markdown 排版成完整网页；MD 文件保留 Markdown 原文。"
    "不要把文件正文仅贴到聊天里代替附件；成功发送后只做简短提示，不重复全文。"
    "失败或送达不确定时如实说明，不声称文件已发送，不自动重发。",
)
plugin.agent_instruction(
    'image_delivery', tools=['render_image'],
    text='用户要求图片时，先完成并整理答复，再对完整内容调用 render_image 一次；'
         '不要把草稿或逐段内容分次渲染。图片发送后不再重复图片中的长文。'
         '摘要默认控制在一页，用户明确要求详细分析时保留必要章节和细节。',
)
plugin.agent_instruction(
    'tree_delivery', tools=['render_image'],
    text='用户要求结构树或目录结构图片时，使用 render_image 的 format=tree。'
         '使用标准 ├──、└──、│   树枝缩进，目录以 / 结尾，文件名与说明之间用两个以上空格分隔。'
         '不要手动换行长文件名或用空格对齐说明，排版器会自动处理。',
)
plugin.agent_instruction(
    "diagram_delivery", tools=["render_diagram"],
    text="用户要求架构图、模块关系图、调用链、分层图或流程图时，先依据已确认的代码或资料整理层级与关系，"
         "写成 Mermaid flowchart（每层用 subgraph，节点名保持简短，边按需加说明），再调用 render_diagram 一次绘制。"
         "不要用 render_image 的代码块、ASCII 字符画或结构树代替架构图。"
         "图内节点建议不超过 20 个；复杂系统按主题拆图，不要把所有细节塞进一张。发送后只做简短说明，不重复图内文字。",
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


image_parameters = schema(format=modes("markdown", "plain", "tree"))
image_parameters["required"] = ["text"]
image_parameters["properties"]["format"]["default"] = "markdown"


@plugin.on_ai_tool(
    "render_image",
    effect="side_effecting",
    capabilities=("qq.send",),
    title="文本转图片",
    timeout=60,
    description="将完整答复排版为 PNG 长图并发送到当前会话。默认 format=markdown，支持标题、列表、引用、表格、代码块和链接文字；"
    "结构树使用 format=tree，自动绘制树枝、区分目录、分列排版文件名与说明；Markdown 中的树形代码块也会自动美化。"
    "format=plain 逐字显示原文。最多 20 个排版页，合成一张连续长图；表格最多 12 列。"
    "图片显示替代文字，不加载本地或远程资源；HTML 按原文显示。"
    "用户明确要图片时使用；普通短答、简单列表或代码片段直接文字回复。"
    "先整理完整内容，一次调用，不发送草稿或逐段调用。摘要默认精炼；用户要求全文或详细版本时保持内容完整。"
    "发送成功后不要再在聊天里重复图片中的长文。",
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


diagram_parameters = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 6000,
            "description": "Mermaid flowchart 文本，例如 flowchart TD 后接节点与连线",
        },
        "title": {"type": "string", "maxLength": 120, "description": "可选图标题"},
    },
    "required": ["text"],
    "additionalProperties": False,
}


@plugin.on_ai_tool(
    "render_diagram",
    effect="side_effecting",
    capabilities=("qq.send",),
    title="绘制架构图",
    timeout=60,
    max_output_chars=2000,
    description=(
        "把 Mermaid flowchart 文本绘制成一张 PNG 架构图并发送到当前会话。"
        "支持 flowchart TD/LR、节点 A[名称]/A(圆角)/A{判断}/A[(数据库)]、"
        "连线 --> / -.-> / ==> / ---、边标签 -->|说明|、subgraph … end 分组（可嵌套）。"
        "适合系统架构、模块关系、调用链和分层图。示例：flowchart TD\n"
        "subgraph 前端\nWeb[Web]\nend\nWeb --> API[API]\nAPI --> DB[(数据库)]。"
        "先整理完整图再调用一次；发送后不要重复图内文字。"
    ),
    parameters=diagram_parameters,
)
async def render_diagram(ctx: Context, text: str, title: str = "") -> dict:
    settings = ctx.settings
    try:
        image_data = await asyncio.to_thread(
            render_diagram_image,
            text,
            title=title,
            font_path=str(settings.get("font_path") or ""),
            font_size=int(settings.get("font_size", 24)),
        )
    except (ValueError, ImportError) as exc:
        return {"error": "缺少图片渲染依赖，请更新安装 FLLBot" if isinstance(exc, ImportError) else str(exc)}
    segments = [MessageSegment.image("base64://" + base64.b64encode(image_data).decode("ascii"))]
    summary = title.strip() or "架构图"
    sent = await send_media_reply(ctx, segments, summary, require_ai=True)
    return {"sent": sent, "images": 1 if sent else 0}


def file_parameters(*, html: bool = False) -> dict:
    parameters = schema(filename={
        "type": "string", "minLength": 1, "maxLength": 120,
        "description": "可选附件名，仅文件名，不能包含路径；省略扩展名时自动补齐",
    })
    parameters["required"] = ["text"]
    if html:
        parameters["properties"]["format"] = {**modes("markdown", "plain"), "default": "markdown"}
    return parameters


async def send_document(ctx: Context, text: str, kind: str, filename: str, *, format: str = "markdown") -> dict:
    try:
        name, payload = await asyncio.to_thread(export_file, text, kind, filename, format=format)
    except (ValueError, ImportError) as exc:
        return {"error": "缺少 HTML 排版依赖，请更新安装 FLLBot" if isinstance(exc, ImportError) else str(exc)}
    segments = [MessageSegment.file("base64://" + base64.b64encode(payload).decode("ascii"), name=name)]
    sent = await send_media_reply(ctx, segments, text, require_ai=True)
    return {"sent": sent, "files": 1 if sent else 0, "filename": name, "bytes": len(payload)}


@plugin.on_ai_tool(
    "send_html_file", title="文本转 HTML 文件", effect="side_effecting",
    capabilities=("qq.send",), timeout=60,
    description="将完整文本转成 UTF-8 HTML 附件，发送到当前群聊或私聊。"
    "默认将 Markdown 标题、列表、代码块和表格排版成可离线打开的完整网页；format=plain 原样显示文本。"
    "不执行原始 HTML，不加载图片或其他外部资源。filename 可选，默认 document.html。"
    "先整理完整内容再调用一次；成功后不要在聊天中重复文件全文。",
    parameters=file_parameters(html=True),
)
async def send_html_file(ctx: Context, text: str, filename: str = "document.html", format: str = "markdown") -> dict:
    return await send_document(ctx, text, "html", filename, format=format)


@plugin.on_ai_tool(
    "send_markdown_file", title="文本转 Markdown 文件", effect="side_effecting",
    capabilities=("qq.send",), timeout=60,
    description="将完整 Markdown 或纯文本原样保存为 UTF-8 .md 附件，发送到当前群聊或私聊。"
    "保留换行、代码和 Markdown 符号，不渲染、不摘要、不截断。filename 可选，默认 document.md。"
    "先整理完整内容再调用一次；成功后不要在聊天中重复文件全文。",
    parameters=file_parameters(),
)
async def send_markdown_file(ctx: Context, text: str, filename: str = "document.md") -> dict:
    return await send_document(ctx, text, "md", filename)
