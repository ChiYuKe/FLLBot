"""Plugin: docs | Role: Office generation, validation and retention | Metadata: plugin.toml.

Word / Excel / PPT 都只在受控结构上生成：只接受纯文本、数字与布尔值，通过
python-docx / openpyxl / python-pptx 的公开 API 写入，不接受任意 XML、图片、超链接或模板。
生成结果先写成真实文件，再用对应库重新打开校验，最后交给插件发送；失败时删除未完成的临时文件。
"""

from __future__ import annotations

import io
import math
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

MAX_TITLE_CHARS = 200
MAX_FILENAME_STEM = 60
EXTENSIONS = ("docx", "xlsx", "pptx")

# 只剥离“看起来像扩展名”的后缀：报告.docx、报告.pdf 会换成本工具要求的扩展名，
# 但 v1.2报告 这种正文里的点不会被误删。
_KNOWN_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,8}$")
# 文件名只允许中文、字母、数字、空格、点与少量安全标点，其余字符一律拒绝。
_UNSAFE_CHARS = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff\- ()（）·.]")
# XML 1.0 不允许的控制字符（保留制表、换行与回车）。
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SHEET_INVALID = re.compile(r"[\\/:*?\[\]]")
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


class DocsError(Exception):
    """带固定原因码的可读错误；message 可直接返回给模型和群友。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Limits:
    """一次生成使用的数量与体积上限；由插件配置转换成实例。"""

    max_chars: int = 50_000
    max_blocks: int = 60
    max_slides: int = 30
    max_rows: int = 2_000
    max_cols: int = 50
    max_cells: int = 20_000
    max_cell_chars: int = 500
    max_bullets: int = 50
    max_file_bytes: int = 8 * 1024 * 1024
    retention_days: int = 3
    font_name: str = "微软雅黑"


@dataclass(frozen=True)
class GeneratedFile:
    """已写入磁盘并通过重新打开校验的生成文件。"""

    path: Path
    filename: str
    payload: bytes
    detail: dict[str, Any]


def safe_font_name(value: Any) -> str:
    """中文字体名只保留可打印字符；空值回退到默认字体。"""
    text = "".join(char for char in str(value or "") if char.isprintable()).strip()
    return text[:60] or "微软雅黑"


def sanitize_filename(value: Any, extension: str, *, fallback: str = "文档") -> str:
    """清洗附件文件名并强制扩展名。

    ``value`` 是模型显式提供的文件名，出现路径、``..``、隐藏名或非安全字符时直接拒绝；
    ``value`` 为空时才使用 ``fallback``（通常是标题）生成一个安全文件名。
    """
    if extension not in EXTENSIONS:
        raise ValueError(f"不支持的扩展名：{extension}")
    raw = str(value or "").strip()
    explicit = bool(raw)
    candidate = raw if explicit else str(fallback or "文档")
    candidate = " ".join(candidate.split())
    if explicit:
        if not candidate:
            raise DocsError("docs_filename_empty", "文件名不能为空，请提供标题或文件名。")
        if any(char in candidate for char in ("/", "\\", ":", "\x00")):
            raise DocsError("docs_filename_invalid", "文件名不能包含路径分隔符、盘符或冒号。")
        if ".." in candidate:
            raise DocsError("docs_filename_invalid", "文件名不能包含 ..。")
        if candidate.startswith("."):
            raise DocsError("docs_filename_invalid", "文件名不能以点开头（隐藏文件）。")
    stem = _KNOWN_SUFFIX.sub("", candidate).rstrip(" .")
    if explicit:
        if not stem:
            raise DocsError("docs_filename_empty", "文件名不能只有扩展名，请补充名称。")
        if _UNSAFE_CHARS.search(stem):
            raise DocsError("docs_filename_invalid", "文件名只能包含中文、字母、数字、空格和 -_.()（）·。")
        if stem.casefold() in _RESERVED_NAMES:
            raise DocsError("docs_filename_invalid", "文件名是系统保留名，请换一个。")
        stem = stem[:MAX_FILENAME_STEM].rstrip(" .")
    else:
        stem = _UNSAFE_CHARS.sub("", stem)[:MAX_FILENAME_STEM].strip(" .") or "文档"
    if not stem:
        raise DocsError("docs_filename_empty", "文件名不能为空，请提供标题或文件名。")
    return f"{stem}.{extension}"


def sanitize_sheet_name(value: Any) -> str:
    """工作表名去掉 Excel 禁止字符并截断到 31 字符。"""
    text = " ".join(str(value or "").split())
    text = _SHEET_INVALID.sub("", text).strip("' ")
    return text[:31].rstrip("' ") or "Sheet1"


# --------------------------------------------------------------------------- #
# 通用校验
# --------------------------------------------------------------------------- #
def _check_control(text: str, label: str, code: str = "docs_content_invalid") -> None:
    if _CONTROL_CHARS.search(text):
        raise DocsError(code, f"{label}包含不可见的控制字符。")


def _text(value: Any, label: str, limits: Limits, used: list[int], *, cap: int | None = None) -> str:
    if not isinstance(value, str):
        raise DocsError("docs_content_invalid", f"{label}必须是字符串。")
    text = value.strip()
    if not text:
        raise DocsError("docs_content_invalid", f"{label}不能为空。")
    _check_control(text, label)
    limit = min(cap, limits.max_chars) if cap is not None else limits.max_chars
    if len(text) > limit:
        raise DocsError("docs_text_too_long", f"{label}超过 {limit} 字上限。")
    used[0] += len(text)
    if used[0] > limits.max_chars:
        raise DocsError("docs_text_too_long", f"正文总字数超过 {limits.max_chars} 字上限。")
    return text


def _line(value: Any, label: str, limits: Limits, used: list[int], *, cap: int | None = None) -> str:
    """单行文本：折叠空白，适合标题、章节标题与要点。"""
    text = _text(value, label, limits, used, cap=cap)
    return " ".join(text.split())


def _cell_text(value: Any, label: str, limits: Limits) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DocsError("docs_cell_invalid", f"{label}不能是 NaN 或无穷大。")
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        _check_control(text, label, "docs_cell_invalid")
        if len(text) > limits.max_cell_chars:
            raise DocsError("docs_cell_invalid", f"{label}超过 {limits.max_cell_chars} 字上限。")
        return text
    raise DocsError("docs_cell_invalid", f"{label}只允许文本、数字或布尔值。")


def _table_cell(value: Any, label: str, limits: Limits, used: list[int]) -> str:
    text = _cell_text(value, label, limits)
    used[0] += len(text)
    if used[0] > limits.max_chars:
        raise DocsError("docs_text_too_long", f"正文总字数超过 {limits.max_chars} 字上限。")
    return text


def _table(value: Any, label: str, limits: Limits, used: list[int]) -> tuple[list[str], list[list[str]], int]:
    if not isinstance(value, dict):
        raise DocsError("docs_content_invalid", f"{label}必须是对象。")
    unknown = set(value) - {"headers", "rows"}
    if unknown:
        raise DocsError("docs_content_invalid", f"{label}包含不支持的字段：{', '.join(sorted(unknown))}。")
    raw_headers = value.get("headers") or []
    if not isinstance(raw_headers, list):
        raise DocsError("docs_content_invalid", f"{label}的 headers 必须是数组。")
    if len(raw_headers) > limits.max_cols:
        raise DocsError("docs_limits_exceeded", f"{label}表头超过 {limits.max_cols} 列上限。")
    headers = [
        _table_cell(item, f"{label}表头第 {index} 列", limits, used)
        for index, item in enumerate(raw_headers, 1)
    ]
    raw_rows = value.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise DocsError("docs_content_invalid", f"{label}的 rows 至少需要一行。")
    if len(raw_rows) > limits.max_rows:
        raise DocsError("docs_limits_exceeded", f"{label}超过 {limits.max_rows} 行上限。")
    columns = len(headers)
    rows: list[list[str]] = []
    for row_index, raw_row in enumerate(raw_rows, 1):
        if not isinstance(raw_row, list):
            raise DocsError("docs_content_invalid", f"{label}第 {row_index} 行必须是数组。")
        if not raw_row:
            raise DocsError("docs_content_invalid", f"{label}第 {row_index} 行不能为空。")
        if len(raw_row) > limits.max_cols:
            raise DocsError("docs_limits_exceeded", f"{label}第 {row_index} 行超过 {limits.max_cols} 列上限。")
        columns = columns or len(raw_row)
        if len(raw_row) != columns:
            raise DocsError("docs_content_invalid", f"{label}各行列数必须一致。")
        rows.append(
            [
                _table_cell(item, f"{label}第 {row_index} 行第 {column_index} 列", limits, used)
                for column_index, item in enumerate(raw_row, 1)
            ]
        )
    return headers, rows, columns


# --------------------------------------------------------------------------- #
# Word
# --------------------------------------------------------------------------- #
def normalize_docx(title: Any, sections: Any, limits: Limits) -> tuple[str, list[tuple[Any, ...]]]:
    used = [0]
    normalized_title = _line(title, "标题", limits, used, cap=MAX_TITLE_CHARS)
    if not isinstance(sections, list) or not sections:
        raise DocsError("docs_content_invalid", "sections 至少需要一个章节。")
    if len(sections) > limits.max_blocks:
        raise DocsError("docs_limits_exceeded", f"章节数超过 {limits.max_blocks} 上限。")
    blocks: list[tuple[Any, ...]] = []
    for index, section in enumerate(sections, 1):
        if not isinstance(section, dict):
            raise DocsError("docs_content_invalid", f"第 {index} 个章节必须是对象。")
        unknown = set(section) - {"heading", "level", "paragraph", "bullets", "table"}
        if unknown:
            raise DocsError("docs_content_invalid", f"第 {index} 个章节包含不支持的字段：{', '.join(sorted(unknown))}。")
        kinds = [kind for kind in ("heading", "paragraph", "bullets", "table") if section.get(kind) is not None]
        if len(kinds) != 1:
            raise DocsError(
                "docs_content_invalid",
                f"第 {index} 个章节必须且只能设置 heading/paragraph/bullets/table 之一。",
            )
        kind = kinds[0]
        value = section[kind]
        if kind == "heading":
            level = section.get("level", 1)
            if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 3:
                raise DocsError("docs_content_invalid", f"第 {index} 个章节的 level 必须是 1–3。")
            blocks.append(("heading", _line(value, f"第 {index} 个标题", limits, used, cap=MAX_TITLE_CHARS), level))
        elif kind == "paragraph":
            blocks.append(("paragraph", _text(value, f"第 {index} 段正文", limits, used)))
        elif kind == "bullets":
            if not isinstance(value, list) or not value:
                raise DocsError("docs_content_invalid", f"第 {index} 组要点必须是非空数组。")
            if len(value) > limits.max_bullets:
                raise DocsError("docs_limits_exceeded", f"第 {index} 组要点超过 {limits.max_bullets} 条上限。")
            items = tuple(
                _line(item, f"第 {index} 组第 {bullet_index} 条要点", limits, used)
                for bullet_index, item in enumerate(value, 1)
            )
            blocks.append(("bullets", items))
        else:
            headers, rows, columns = _table(value, f"第 {index} 个表格", limits, used)
            blocks.append(("table", headers, rows, columns))
    return normalized_title, blocks


def _docx() -> Any:
    try:
        from docx import Document
        from docx.oxml.ns import qn
    except ImportError as exc:
        raise DocsError("docs_dependency_missing", "缺少 python-docx，无法生成 Word 文档。") from exc
    return SimpleNamespace(Document=Document, qn=qn)


def _set_docx_style_font(style: Any, qn: Any, font: str) -> None:
    style.font.name = font
    fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    fonts.set(qn("w:eastAsia"), font)


def _apply_docx_font(document: Any, qn: Any, font: str) -> None:
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3", "List Bullet"):
        try:
            style = document.styles[name]
        except KeyError:
            continue
        _set_docx_style_font(style, qn, font)


def _try_style(paragraph: Any, name: str) -> None:
    try:
        paragraph.style = name
    except (KeyError, ValueError):
        return


def _try_table_style(table: Any, name: str) -> None:
    try:
        table.style = name
    except (KeyError, ValueError):
        return


def build_docx(title: str, blocks: list[tuple[Any, ...]], limits: Limits) -> tuple[bytes, dict[str, Any]]:
    api = _docx()
    document = api.Document()
    _apply_docx_font(document, api.qn, limits.font_name)
    document.add_heading(title, level=0)
    tables = 0
    for block in blocks:
        kind = block[0]
        if kind == "heading":
            document.add_heading(block[1], level=block[2])
        elif kind == "paragraph":
            document.add_paragraph(block[1])
        elif kind == "bullets":
            for item in block[1]:
                paragraph = document.add_paragraph(item)
                _try_style(paragraph, "List Bullet")
        else:
            headers, rows, columns = block[1], block[2], block[3]
            table = document.add_table(rows=0, cols=columns)
            _try_table_style(table, "Table Grid")
            if headers:
                cells = table.add_row().cells
                for column, text in enumerate(headers):
                    cells[column].text = text
                    for paragraph in cells[column].paragraphs:
                        for run in paragraph.runs:
                            run.bold = True
            for row in rows:
                cells = table.add_row().cells
                for column, text in enumerate(row):
                    cells[column].text = text
            tables += 1
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue(), {"sections": len(blocks), "tables": tables}


def validate_docx(path: Path, title: str) -> None:
    api = _docx()
    document = api.Document(str(path))
    texts = [paragraph.text for paragraph in document.paragraphs]
    if not texts or texts[0] != title:
        raise DocsError("docs_generate_failed", "生成的 Word 文件无法通过重新打开校验。")


def write_docx(directory: Path, filename: str, title: Any, sections: Any, limits: Limits) -> GeneratedFile:
    normalized_title, blocks = normalize_docx(title, sections, limits)
    payload, detail = build_docx(normalized_title, blocks, limits)
    return _commit(
        directory,
        filename,
        payload,
        detail,
        limits,
        lambda path: validate_docx(path, normalized_title),
    )


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #
def _openpyxl() -> Any:
    try:
        from openpyxl import Workbook, load_workbook
        from openpyxl.styles import Alignment, Font
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise DocsError("docs_dependency_missing", "缺少 openpyxl，无法生成 Excel 表格。") from exc
    return SimpleNamespace(
        Workbook=Workbook,
        load_workbook=load_workbook,
        Font=Font,
        Alignment=Alignment,
        get_column_letter=get_column_letter,
    )


def _primitive(value: Any, row: int, column: int, limits: Limits) -> Any:
    label = f"第 {row} 行第 {column} 列"
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DocsError("docs_cell_invalid", f"{label}不能是 NaN 或无穷大。")
        return value
    if isinstance(value, str):
        _check_control(value, label, "docs_cell_invalid")
        if len(value) > limits.max_cell_chars:
            raise DocsError("docs_cell_invalid", f"{label}超过 {limits.max_cell_chars} 字上限。")
        return value
    raise DocsError("docs_cell_invalid", f"{label}只允许文本、数字或布尔值。")


def normalize_xlsx(rows: Any, limits: Limits) -> tuple[list[list[Any]], int]:
    if not isinstance(rows, list) or not rows:
        raise DocsError("docs_content_invalid", "rows 至少需要一行。")
    if len(rows) > limits.max_rows:
        raise DocsError("docs_limits_exceeded", f"表格行数超过 {limits.max_rows} 上限。")
    normalized: list[list[Any]] = []
    columns = 0
    cells = 0
    for row_index, raw_row in enumerate(rows, 1):
        if not isinstance(raw_row, list):
            raise DocsError("docs_content_invalid", f"第 {row_index} 行必须是数组。")
        if not raw_row:
            raise DocsError("docs_content_invalid", f"第 {row_index} 行不能为空。")
        if len(raw_row) > limits.max_cols:
            raise DocsError("docs_limits_exceeded", f"第 {row_index} 行超过 {limits.max_cols} 列上限。")
        cells += len(raw_row)
        if cells > limits.max_cells:
            raise DocsError("docs_limits_exceeded", f"单元格总数超过 {limits.max_cells} 上限。")
        columns = max(columns, len(raw_row))
        normalized.append(
            [_primitive(value, row_index, column, limits) for column, value in enumerate(raw_row, 1)]
        )
    return normalized, columns


def _display_width(value: Any) -> int:
    return len(str(value)) if value is not None else 0


def _set_xlsx_cell(cell: Any, value: Any) -> None:
    cell.value = value
    # openpyxl 会把以 = 开头的字符串当公式保存；这里强制按纯文本写，避免公式注入。
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"


def build_xlsx(sheet_name: str, rows: list[list[Any]], header: bool, limits: Limits) -> tuple[bytes, dict[str, Any]]:
    api = _openpyxl()
    workbook = api.Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    data_rows = rows[1:] if header else rows
    columns = max((len(row) for row in rows), default=0)
    widths = [0] * columns
    if header:
        for column, value in enumerate(rows[0], 1):
            cell = sheet.cell(row=1, column=column)
            _set_xlsx_cell(cell, value)
            cell.font = api.Font(name=limits.font_name, bold=True)
            cell.alignment = api.Alignment(vertical="center", wrap_text=True)
            widths[column - 1] = max(widths[column - 1], _display_width(value))
        sheet.freeze_panes = "A2"
    for row_index, row in enumerate(data_rows, 2 if header else 1):
        for column, value in enumerate(row, 1):
            cell = sheet.cell(row=row_index, column=column)
            _set_xlsx_cell(cell, value)
            cell.font = api.Font(name=limits.font_name)
            widths[column - 1] = max(widths[column - 1], _display_width(value))
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[api.get_column_letter(column)].width = min(60, max(10, width * 1.2 + 2))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), {"rows": len(data_rows), "columns": columns, "header": header}


def validate_xlsx(path: Path, sheet_name: str, min_rows: int) -> None:
    api = _openpyxl()
    workbook = api.load_workbook(str(path))
    try:
        if sheet_name not in workbook.sheetnames:
            raise DocsError("docs_generate_failed", "生成的 Excel 文件无法通过重新打开校验。")
        sheet = workbook[sheet_name]
        if sheet.max_row < min_rows:
            raise DocsError("docs_generate_failed", "生成的 Excel 文件无法通过重新打开校验。")
    finally:
        workbook.close()


def write_xlsx(
    directory: Path,
    filename: str,
    sheet_name: Any,
    rows: Any,
    header: bool,
    limits: Limits,
) -> GeneratedFile:
    normalized, _columns = normalize_xlsx(rows, limits)
    title = sanitize_sheet_name(sheet_name)
    payload, detail = build_xlsx(title, normalized, bool(header), limits)
    return _commit(
        directory,
        filename,
        payload,
        detail,
        limits,
        lambda path: validate_xlsx(path, title, 1),
    )


# --------------------------------------------------------------------------- #
# PowerPoint
# --------------------------------------------------------------------------- #
def _pptx() -> Any:
    try:
        from pptx import Presentation
        from pptx.oxml.ns import qn
    except ImportError as exc:
        raise DocsError("docs_dependency_missing", "缺少 python-pptx，无法生成 PPT 演示文稿。") from exc
    return SimpleNamespace(Presentation=Presentation, qn=qn)


def normalize_pptx(title: Any, slides: Any, limits: Limits) -> tuple[str, list[tuple[str, list[str]]]]:
    used = [0]
    normalized_title = _line(title, "标题", limits, used, cap=MAX_TITLE_CHARS)
    if not isinstance(slides, list) or not slides:
        raise DocsError("docs_content_invalid", "slides 至少需要一页。")
    if len(slides) > limits.max_slides:
        raise DocsError("docs_limits_exceeded", f"页数超过 {limits.max_slides} 上限。")
    normalized: list[tuple[str, list[str]]] = []
    for index, slide in enumerate(slides, 1):
        if not isinstance(slide, dict):
            raise DocsError("docs_content_invalid", f"第 {index} 页必须是对象。")
        unknown = set(slide) - {"title", "bullets"}
        if unknown:
            raise DocsError("docs_content_invalid", f"第 {index} 页包含不支持的字段：{', '.join(sorted(unknown))}。")
        slide_title = _line(slide.get("title"), f"第 {index} 页标题", limits, used, cap=MAX_TITLE_CHARS)
        raw_bullets = slide.get("bullets") or []
        if not isinstance(raw_bullets, list):
            raise DocsError("docs_content_invalid", f"第 {index} 页的 bullets 必须是数组。")
        if len(raw_bullets) > limits.max_bullets:
            raise DocsError("docs_limits_exceeded", f"第 {index} 页要点超过 {limits.max_bullets} 条上限。")
        bullets = [
            _line(item, f"第 {index} 页第 {bullet_index} 条要点", limits, used)
            for bullet_index, item in enumerate(raw_bullets, 1)
        ]
        normalized.append((slide_title, bullets))
    return normalized_title, normalized


def _apply_pptx_run_font(run: Any, qn: Any, font: str) -> None:
    run.font.name = font
    properties = run._r.get_or_add_rPr()
    east_asian = properties.find(qn("a:ea"))
    if east_asian is None:
        east_asian = properties.makeelement(qn("a:ea"), {})
        properties.append(east_asian)
    east_asian.set("typeface", font)


def _fill_pptx_title(shape: Any, text: str, qn: Any, font: str) -> None:
    shape.text = text
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            _apply_pptx_run_font(run, qn, font)


def build_pptx(
    title: str,
    slides: list[tuple[str, list[str]]],
    limits: Limits,
) -> tuple[bytes, dict[str, Any]]:
    api = _pptx()
    presentation = api.Presentation()
    cover = presentation.slides.add_slide(presentation.slide_layouts[0])
    _fill_pptx_title(cover.shapes.title, title, api.qn, limits.font_name)
    for slide_title, bullets in slides:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        _fill_pptx_title(slide.shapes.title, slide_title, api.qn, limits.font_name)
        body = slide.placeholders[1].text_frame
        for index, bullet in enumerate(bullets):
            paragraph = body.paragraphs[0] if index == 0 else body.add_paragraph()
            run = paragraph.add_run()
            run.text = bullet
            _apply_pptx_run_font(run, api.qn, limits.font_name)
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue(), {"slides": len(slides) + 1}


def validate_pptx(path: Path, title: str, slide_count: int) -> None:
    api = _pptx()
    presentation = api.Presentation(str(path))
    if len(presentation.slides) != slide_count + 1:
        raise DocsError("docs_generate_failed", "生成的 PPT 文件无法通过重新打开校验。")
    cover_title = presentation.slides[0].shapes.title
    if cover_title is None or cover_title.text != title:
        raise DocsError("docs_generate_failed", "生成的 PPT 文件无法通过重新打开校验。")


def write_pptx(directory: Path, filename: str, title: Any, slides: Any, limits: Limits) -> GeneratedFile:
    normalized_title, normalized_slides = normalize_pptx(title, slides, limits)
    payload, detail = build_pptx(normalized_title, normalized_slides, limits)
    return _commit(
        directory,
        filename,
        payload,
        detail,
        limits,
        lambda path: validate_pptx(path, normalized_title, len(normalized_slides)),
    )


# --------------------------------------------------------------------------- #
# 落盘、校验与清理
# --------------------------------------------------------------------------- #
def _commit(
    directory: Path,
    filename: str,
    payload: bytes,
    detail: dict[str, Any],
    limits: Limits,
    validate: Callable[[Path], None],
) -> GeneratedFile:
    if len(payload) > limits.max_file_bytes:
        raise DocsError(
            "docs_too_large",
            f"生成文件约 {len(payload) / 1024 / 1024:.1f} MB，"
            f"超过 {limits.max_file_bytes / 1024 / 1024:.1f} MB 上限。",
        )
    directory.mkdir(parents=True, exist_ok=True)
    # 前缀避免并发同名覆盖；附件名仍使用清洗后的用户文件名。
    path = directory / f"{uuid.uuid4().hex[:12]}_{filename}"
    try:
        path.write_bytes(payload)
        validate(path)
    except DocsError:
        path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise DocsError("docs_filesystem_error", "写入生成文件失败，请检查磁盘空间与权限。") from exc
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise DocsError("docs_generate_failed", "生成的文件无法通过校验，未发送。") from exc
    return GeneratedFile(path=path, filename=filename, payload=payload, detail=detail)


def cleanup(directory: Path, retention_days: int, keep: Path | None = None) -> int:
    """删除超过保留期的生成文件；尽力而为，失败不阻塞发送。"""
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86_400
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for path in entries:
        if keep is not None and path == keep:
            continue
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
