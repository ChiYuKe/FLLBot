"""Plugin: docs | Role: settings, AI tools and attachment delivery | Metadata: plugin.toml.

三个工具都声明 ``qq.send_file``：宿主沙盒会在自动参与（未 @、未叫到）时拒绝，模型也必须在
「智能体 → 允许调用的工具」里被授权后才看得见。文件生成在插件 data_dir 的 files/ 子目录，
先写成真实文件并用对应库重新打开校验，发送失败时删除，不留下半成品。
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fllbot.plugin import Context, MessageSegment, Plugin, send_media_reply

from . import service as core

plugin = Plugin.from_manifest(__file__)

_KIND_LABELS = {"docx": "Word 文档", "xlsx": "Excel 表格", "pptx": "PPT 演示文稿"}

plugin.setting(
    "enabled",
    type="bool",
    label="启用文档生成",
    default=True,
    group_scoped=True,
    description="关闭后本群不会生成或发送 Word/Excel/PPT；不影响其他群。",
)
plugin.setting(
    "max_file_mb",
    type="float",
    label="单文件大小上限（MB）",
    default=8.0,
    min=1.0,
    max=30.0,
    description="生成文件超过上限直接拒绝，不写入也不发送；同时受 QQ 侧附件能力限制。",
)
plugin.setting(
    "max_chars",
    type="int",
    label="正文总字数上限",
    default=50_000,
    min=100,
    max=500_000,
    description="标题、正文、要点与表格单元格文字合计超过上限时拒绝生成，避免超长内容拖慢发送。",
)
plugin.setting(
    "max_blocks",
    type="int",
    label="Word 章节上限",
    default=60,
    min=1,
    max=500,
    description="sections 的最大数量；表格也按一个章节计算。",
)
plugin.setting(
    "max_slides",
    type="int",
    label="PPT 内容页上限",
    default=30,
    min=1,
    max=100,
    description="不含封面页的最大页数。",
)
plugin.setting(
    "max_rows",
    type="int",
    label="表格行数上限",
    default=2_000,
    min=1,
    max=10_000,
    description="Excel 与 Word 表格的最大行数（表头行不计入 Excel 数据行上限）。",
)
plugin.setting(
    "max_cols",
    type="int",
    label="表格列数上限",
    default=50,
    min=1,
    max=100,
    description="Excel 与 Word 表格的最大列数。",
)
plugin.setting(
    "max_cells",
    type="int",
    label="Excel 单元格总数上限",
    default=20_000,
    min=1,
    max=200_000,
    description="所有行列展开后的单元格总数上限，含表头行。",
)
plugin.setting(
    "max_cell_chars",
    type="int",
    label="单元格字数上限",
    default=500,
    min=10,
    max=2_000,
    description="Excel 与 Word 表格单个单元格的最大字数。",
)
plugin.setting(
    "max_bullets",
    type="int",
    label="单组要点条数上限",
    default=50,
    min=1,
    max=200,
    description="Word 的 bullets 一组的条数、PPT 单页的要点条数上限。",
)
plugin.setting(
    "retention_days",
    type="int",
    label="生成文件保留天数",
    default=3,
    min=1,
    max=30,
    description="发送成功后按天清理 data/plugins/docs/files 里的旧文件；最近发送的文件不受影响。",
)
plugin.setting(
    "font_name",
    type="string",
    label="中文字体",
    default="微软雅黑",
    description="写入 Word/Excel/PPT 的西文与中文字体名；留空或非法时回退到微软雅黑。",
)

plugin.agent_instruction(
    "document_generation",
    tools=["write_docx", "write_xlsx", "write_pptx"],
    text=(
        "群友明确要求生成 Word 文档、Excel 表格或 PPT 演示文稿、并把文件发出来时，"
        "才调用 docs__write_docx / docs__write_xlsx / docs__write_pptx；自动参与或未被要求时不要主动生成。"
        "内容只使用纯文本、数字与布尔值：不支持图片、超链接、公式、任意 XML 或自定义模板；"
        "需要排版时用 sections 里的标题/正文/要点/表格组合，不要用 Markdown 或 HTML 模拟格式。"
        "filename 只填文件名（可以省略，省略时按标题生成），扩展名由插件强制；"
        "不要填路径、盘符或 ..。长报告先分章节组织，表格保持列数一致。"
        "文件以 QQ 文件附件发送；返回 docs_not_delivered 时如实告诉群友没有发出去，不要谎称已发送。"
    ),
)


def _limits(settings: Any) -> core.Limits:
    return core.Limits(
        max_chars=int(settings.get("max_chars") or 50_000),
        max_blocks=int(settings.get("max_blocks") or 60),
        max_slides=int(settings.get("max_slides") or 30),
        max_rows=int(settings.get("max_rows") or 2_000),
        max_cols=int(settings.get("max_cols") or 50),
        max_cells=int(settings.get("max_cells") or 20_000),
        max_cell_chars=int(settings.get("max_cell_chars") or 500),
        max_bullets=int(settings.get("max_bullets") or 50),
        max_file_bytes=max(1, int(float(settings.get("max_file_mb") or 8.0) * 1024 * 1024)),
        retention_days=int(settings.get("retention_days") or 3),
        font_name=core.safe_font_name(settings.get("font_name")),
    )


def _error(exc: core.DocsError) -> dict[str, Any]:
    return {"error": exc.code, "message": exc.message}


async def _send_document(
    ctx: Context,
    kind: str,
    filename: str,
    fallback: str,
    builder: Callable[[Path, str, core.Limits], core.GeneratedFile],
) -> dict[str, Any]:
    """生成、校验、发送一个 Office 附件；失败只返回可读原因，不发送半成品。"""
    settings = ctx.settings
    if not bool(settings.get("enabled", True)):
        return {"error": "docs_disabled", "message": "文档生成在本群没有启用。"}
    limits = _limits(settings)
    try:
        display_name = core.sanitize_filename(filename, kind, fallback=fallback)
    except core.DocsError as exc:
        return _error(exc)
    directory = ctx.data_dir / "files"
    try:
        generated = await asyncio.to_thread(builder, directory, display_name, limits)
    except core.DocsError as exc:
        return _error(exc)
    except OSError:
        ctx.logger.warning("文档生成写入失败")
        return {"error": "docs_filesystem_error", "message": "写入生成文件失败，请稍后再试。"}
    except Exception:  # noqa: BLE001 - 生成失败不应打断整轮对话
        ctx.logger.exception("文档生成出现未预期错误")
        return {"error": "docs_generate_failed", "message": "文档生成失败，未发送任何文件。"}
    if not ctx.data_current:
        generated.path.unlink(missing_ok=True)
        return {"error": "docs_context_expired", "message": "会话已失效，未发送文件。"}
    payload = generated.payload
    size_kb = max(1, round(len(payload) / 1024))
    summary = f"（{_KIND_LABELS[kind]}：{display_name}，{size_kb} KB）"
    segment = MessageSegment.file("base64://" + base64.b64encode(payload).decode("ascii"), name=display_name)
    try:
        sent = await send_media_reply(ctx, [segment], summary, require_ai=True)
    except Exception:  # noqa: BLE001 - 发送失败要返回可读结果，而不是中断整轮对话
        ctx.logger.exception("文档附件发送失败")
        sent = False
    if not sent:
        generated.path.unlink(missing_ok=True)
        return {
            "sent": False,
            "error": "docs_not_delivered",
            "message": "文件已生成，但没有送达；会话可能已经结束或发送被拒绝。",
        }
    try:
        await asyncio.to_thread(core.cleanup, directory, limits.retention_days, generated.path)
    except Exception:  # noqa: BLE001 - 清理只是尽力而为
        ctx.logger.debug("清理旧文档文件失败")
    return {
        "sent": True,
        "filename": display_name,
        "format": kind,
        "bytes": len(payload),
        **generated.detail,
        "note": "文件已发送；只简短回应一句，不要重复正文内容。",
    }


_DOCX_SECTIONS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "maxItems": 500,
    "items": {
        "type": "object",
        "properties": {
            "heading": {"type": "string", "maxLength": 200, "description": "章节标题文字"},
            "level": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "description": "标题级别 1–3，仅 heading 使用；省略为 1",
            },
            "paragraph": {"type": "string", "maxLength": 500_000, "description": "一段正文，可含换行"},
            "bullets": {
                "type": "array",
                "items": {"type": "string", "maxLength": 2_000},
                "maxItems": 200,
                "description": "要点列表，按顺序逐条显示",
            },
            "table": {
                "type": "object",
                "properties": {
                    "headers": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 500},
                        "maxItems": 100,
                        "description": "表头文字；省略则不设表头",
                    },
                    "rows": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 10_000,
                        "items": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 100,
                            "items": {"type": "string", "maxLength": 2_000},
                        },
                        "description": "表格数据，每行一个数组，各行列数必须一致",
                    },
                },
                "required": ["rows"],
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
    "description": "按顺序排列的章节，每项只设置 heading/paragraph/bullets/table 之一",
}


_DOCX_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200, "description": "文档标题"},
        "sections": _DOCX_SECTIONS_SCHEMA,
        "filename": {
            "type": "string",
            "maxLength": 120,
            "description": "附件文件名（可省略）；不要带路径和 ..，扩展名由插件强制为 .docx",
        },
    },
    "required": ["title", "sections"],
    "additionalProperties": False,
}


_XLSX_ROWS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "maxItems": 10_000,
    "items": {
        "type": "array",
        "minItems": 1,
        "maxItems": 100,
        "items": {"description": "单元格：只允许字符串、数字或布尔值，不支持公式、图片或嵌套结构"},
    },
    "description": "二维数组；header=true 时第一行作为表头，其余行按顺序写入",
}


_XLSX_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sheet_name": {"type": "string", "minLength": 1, "maxLength": 60, "description": "工作表名称"},
        "rows": _XLSX_ROWS_SCHEMA,
        "header": {"type": "boolean", "description": "是否把第一行作为加粗表头并冻结首行，默认 true"},
        "filename": {
            "type": "string",
            "maxLength": 120,
            "description": "附件文件名（可省略）；不要带路径和 ..，扩展名由插件强制为 .xlsx",
        },
    },
    "required": ["sheet_name", "rows"],
    "additionalProperties": False,
}


_PPTX_SLIDES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "maxItems": 100,
    "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200, "description": "本页标题"},
            "bullets": {
                "type": "array",
                "items": {"type": "string", "maxLength": 2_000},
                "maxItems": 200,
                "description": "本页要点，按顺序逐条显示；可以为空数组",
            },
        },
        "required": ["title", "bullets"],
        "additionalProperties": False,
    },
    "description": "内容页列表，封面页由插件自动生成",
}


_PPTX_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200, "description": "演示文稿标题（封面页）"},
        "slides": _PPTX_SLIDES_SCHEMA,
        "filename": {
            "type": "string",
            "maxLength": 120,
            "description": "附件文件名（可省略）；不要带路径和 ..，扩展名由插件强制为 .pptx",
        },
    },
    "required": ["title", "slides"],
    "additionalProperties": False,
}


@plugin.on_ai_tool(
    "write_docx",
    title="生成 Word 文档",
    effect="side_effecting",
    capabilities=("qq.send_file",),
    timeout=120,
    max_output_chars=1200,
    description=(
        "生成 Word（.docx）文档并作为 QQ 文件附件发送到当前会话。"
        "sections 按顺序排列，每项只设置 heading（标题，可带 level 1–3）、paragraph（正文）、"
        "bullets（要点数组）或 table（{headers, rows} 表格）之一；只接受纯文本，"
        "不支持图片、超链接、公式、任意 XML 或模板。filename 可省略，省略时按标题命名，"
        "扩展名强制为 .docx；不要传路径或 ..。"
        "群友明确要求“做个报告/文档/总结发我”时才调用；自动参与会被沙盒拒绝，未授权时模型看不到本工具。"
        "返回 sent=true 表示附件已送达；失败时按 error 与 message 如实说明。"
    ),
    parameters=_DOCX_PARAMETERS,
)
async def write_docx(
    ctx: Context,
    title: str,
    sections: list[dict[str, Any]],
    filename: str = "",
) -> dict[str, Any]:
    return await _send_document(
        ctx,
        "docx",
        filename,
        title,
        lambda directory, name, limits: core.write_docx(directory, name, title, sections, limits),
    )


@plugin.on_ai_tool(
    "write_xlsx",
    title="生成 Excel 表格",
    effect="side_effecting",
    capabilities=("qq.send_file",),
    timeout=120,
    max_output_chars=1200,
    description=(
        "生成 Excel（.xlsx）表格并作为 QQ 文件附件发送到当前会话。"
        "rows 是二维数组，单元格只允许字符串、数字或布尔值；header=true 时第一行作为加粗表头，"
        "并冻结首行、按内容调整列宽。filename 可省略，省略时按工作表名命名，扩展名强制为 .xlsx；"
        "不要传路径或 ..。以 = 开头的文本按纯文本写入，不会变成公式。"
        "群友明确要求“做个表格/统计表发我”时才调用；自动参与会被沙盒拒绝，未授权时模型看不到本工具。"
        "返回 sent=true 表示附件已送达；失败时按 error 与 message 如实说明。"
    ),
    parameters=_XLSX_PARAMETERS,
)
async def write_xlsx(
    ctx: Context,
    sheet_name: str,
    rows: list[list[Any]],
    header: bool = True,
    filename: str = "",
) -> dict[str, Any]:
    return await _send_document(
        ctx,
        "xlsx",
        filename,
        sheet_name,
        lambda directory, name, limits: core.write_xlsx(directory, name, sheet_name, rows, header, limits),
    )


@plugin.on_ai_tool(
    "write_pptx",
    title="生成 PPT 演示文稿",
    effect="side_effecting",
    capabilities=("qq.send_file",),
    timeout=120,
    max_output_chars=1200,
    description=(
        "生成 PowerPoint（.pptx）演示文稿并作为 QQ 文件附件发送到当前会话。"
        "封面页使用 title，slides 是内容页数组，每页含 title 与 bullets（要点数组，可为空）；"
        "只接受纯文本，不支持图片、图表、超链接、公式或自定义版式。filename 可省略，"
        "省略时按标题命名，扩展名强制为 .pptx；不要传路径或 ..。"
        "群友明确要求“做个 PPT/演示文稿发我”时才调用；自动参与会被沙盒拒绝，未授权时模型看不到本工具。"
        "返回 sent=true 表示附件已送达；失败时按 error 与 message 如实说明。"
    ),
    parameters=_PPTX_PARAMETERS,
)
async def write_pptx(
    ctx: Context,
    title: str,
    slides: list[dict[str, Any]],
    filename: str = "",
) -> dict[str, Any]:
    return await _send_document(
        ctx,
        "pptx",
        filename,
        title,
        lambda directory, name, limits: core.write_pptx(directory, name, title, slides, limits),
    )


__all__ = ["plugin", "write_docx", "write_pptx", "write_xlsx"]
