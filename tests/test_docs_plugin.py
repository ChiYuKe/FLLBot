"""docs 插件测试：离线生成 Word/Excel/PPT，并用对应库重新打开校验内容。

不访问网络、不启动 QQ；附件发送由 bot.deliveries.send_media 的假实现截获，
文件生成位置与真实运行一致（data/plugins/docs/files/）。
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import shutil
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation

from fllbot.builtin_plugins.agent.plugin import _get_tools
from fllbot.onebot.models import parse_event
from fllbot.plugin import Context
from fllbot.plugin.agent import instruction_sections
from helpers import FakeConnection, group_message, make_bot

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = [
    {"heading": "背景", "level": 1},
    {"paragraph": "本季度完成多项工作。"},
    {"bullets": ["完成 A 项目", "完成 B 项目"]},
    {"table": {"headers": ["项目", "状态"], "rows": [["A", "完成"], ["B", "进行中"]]}},
]


def load_plugin_package(name: str) -> Any:
    """按宿主的方式导入外置插件包，返回 plugin.py 模块。"""
    package_name = f"fllbot_test_{name}_plugin"
    if package_name in sys.modules:
        return sys.modules[package_name + ".plugin"]
    directory = ROOT / "plugins" / name
    spec = importlib.util.spec_from_file_location(
        package_name, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    assert spec and spec.loader
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    return sys.modules[package_name + ".plugin"]


docs_plugin = load_plugin_package("docs")


@pytest.fixture
async def docs_bot(tmp_path: Path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "docs", plugin_dir / "docs")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    installed = bot.plugins.get("docs")
    assert installed is not None
    try:
        yield bot, installed
    finally:
        await bot.stop()


def spec_of(installed: Any, name: str) -> Any:
    return next(tool for tool in installed.ai_tools if tool.name == name)


def context(bot: Any, installed: Any, text: str = "做个报告发我", *, at: int | None = 10001) -> Context:
    return Context(
        bot=bot,
        connection=FakeConnection(),
        event=parse_event(group_message(text, at=at)),
        plugin=installed,
    )


def capture_send(bot: Any, monkeypatch: pytest.MonkeyPatch, *, result: bool = True) -> AsyncMock:
    sender = AsyncMock(return_value=result)
    monkeypatch.setattr(bot.deliveries, "send_media", sender)
    return sender


def attachment(sender: AsyncMock) -> tuple[str, bytes, str]:
    segments, content = sender.call_args.args[1:3]
    assert len(segments) == 1 and segments[0].type == "file"
    name = str(segments[0].data["name"])
    payload = base64.b64decode(str(segments[0].data["file"]).removeprefix("base64://"))
    return name, payload, content


def data_files(installed: Any) -> list[Path]:
    directory = installed.data_dir / "files"
    return sorted(directory.glob("*")) if directory.is_dir() else []


# --------------------------------------------------------------------------- #
# 包头、工具声明与依赖
# --------------------------------------------------------------------------- #
def test_manifest_and_pyproject_declare_office_dependencies() -> None:
    manifest = tomllib.loads((ROOT / "plugins/docs/plugin.toml").read_text(encoding="utf-8"))
    assert manifest["name"] == "docs"
    assert manifest["schema_version"] == 1 and manifest["api_version"] == 1
    dependencies = {item.split(">")[0]: item for item in manifest["dependencies"]}
    assert set(dependencies) == {"python-docx", "openpyxl", "python-pptx"}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = project["project"]["dependencies"]
    for prefix in ("python-docx>=", "openpyxl>=", "python-pptx>="):
        assert any(item.startswith(prefix) for item in declared), prefix


def test_tools_declare_file_sending_capabilities() -> None:
    tools = {tool.name: tool for tool in docs_plugin.plugin.ai_tools}
    assert set(tools) == {"write_docx", "write_xlsx", "write_pptx"}
    for tool in tools.values():
        assert tool.effect == "side_effecting"
        assert set(tool.capabilities) == {"qq.send_file"}
        assert tool.risk == "write"
        assert tool.display_title
        assert tool.timeout >= 60
    assert tools["write_docx"].parameters["required"] == ["title", "sections"]
    assert tools["write_xlsx"].parameters["required"] == ["sheet_name", "rows"]
    assert tools["write_pptx"].parameters["required"] == ["title", "slides"]


def test_agent_instruction_only_applies_with_all_tools() -> None:
    installed = docs_plugin.plugin
    specs = [tool for tool in installed.ai_tools if tool.name in {"write_docx", "write_xlsx", "write_pptx"}]
    registry = {spec.qualified_name: (installed, spec) for spec in specs}
    definitions = [{"type": "function", "function": {"name": spec.qualified_name}} for spec in specs]
    sections = instruction_sections(registry, definitions)
    assert [section["source"] for section in sections] == ["docs__document_generation"]
    assert instruction_sections(registry, definitions[:-1]) == []


# --------------------------------------------------------------------------- #
# 三种格式：生成后用对应库重新打开
# --------------------------------------------------------------------------- #
async def test_write_docx_generates_reopenable_word_file(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    result = await spec_of(installed, "write_docx").callback(
        context(bot, installed),
        title="季度总结",
        sections=SECTIONS,
        filename="季度总结.docx",
    )
    assert result["sent"] is True
    assert result["format"] == "docx" and result["sections"] == 4 and result["tables"] == 1
    name, payload, content = attachment(sender)
    assert name == "季度总结.docx" and "Word 文档" in content
    assert payload[:2] == b"PK"

    document = Document(io.BytesIO(payload))
    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    assert paragraphs[0] == "季度总结"
    assert "背景" in paragraphs and "本季度完成多项工作。" in paragraphs
    assert "完成 A 项目" in paragraphs
    assert [cell.text for cell in document.tables[0].rows[0].cells] == ["项目", "状态"]
    assert document.tables[0].cell(1, 1).text == "完成"
    assert document.styles["Normal"].font.name == "微软雅黑"

    # 磁盘上的真实文件同样必须能被重新打开，且位于插件数据目录。
    files = data_files(installed)
    assert len(files) == 1 and files[0].parent == installed.data_dir / "files"
    assert files[0].name.endswith("_季度总结.docx")
    assert Document(str(files[0])).paragraphs[0].text == "季度总结"


async def test_write_xlsx_generates_reopenable_workbook(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    rows = [
        ["姓名", "分数", "及格"],
        ["小明", 95, True],
        ["小红", 88.5, False],
        ["注入", "=SUM(B2:B3)", True],
    ]
    result = await spec_of(installed, "write_xlsx").callback(
        context(bot, installed),
        sheet_name="成绩",
        rows=rows,
        header=True,
        filename="成绩.xlsx",
    )
    assert result["sent"] is True
    assert (result["rows"], result["columns"], result["header"]) == (3, 3, True)
    name, payload, _content = attachment(sender)
    assert name == "成绩.xlsx"

    workbook = load_workbook(io.BytesIO(payload))
    sheet = workbook.active
    assert sheet.title == "成绩"
    assert sheet.max_row == 4 and sheet.max_column == 3
    assert sheet.freeze_panes == "A2"
    assert sheet["A1"].value == "姓名" and sheet["A1"].font.bold is True
    assert sheet["A1"].font.name == "微软雅黑"
    assert sheet["B2"].value == 95 and isinstance(sheet["B2"].value, int)
    assert sheet["B3"].value == 88.5
    assert sheet["C2"].value is True and sheet["C3"].value is False
    # 以 = 开头的文本必须按纯文本保存，不能变成公式。
    assert sheet["B4"].value == "=SUM(B2:B3)" and sheet["B4"].data_type == "s"
    assert sheet.column_dimensions["A"].width >= 10

    files = data_files(installed)
    assert len(files) == 1
    reopened = load_workbook(str(files[0]))
    assert reopened["成绩"]["B2"].value == 95
    reopened.close()


async def test_write_pptx_generates_reopenable_presentation(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    slides = [
        {"title": "目标", "bullets": ["提升效率", "降低成本"]},
        {"title": "计划", "bullets": ["第一步：调研", "第二步：落地"]},
    ]
    result = await spec_of(installed, "write_pptx").callback(
        context(bot, installed),
        title="项目汇报",
        slides=slides,
        filename="项目汇报.pptx",
    )
    assert result["sent"] is True and result["slides"] == 3
    name, payload, _content = attachment(sender)
    assert name == "项目汇报.pptx"

    presentation = Presentation(io.BytesIO(payload))
    assert len(presentation.slides) == 3
    assert presentation.slides[0].shapes.title.text == "项目汇报"
    assert presentation.slides[1].shapes.title.text == "目标"
    bullets = [paragraph.text for paragraph in presentation.slides[1].placeholders[1].text_frame.paragraphs]
    assert bullets == ["提升效率", "降低成本"]

    files = data_files(installed)
    assert len(files) == 1
    reopened = Presentation(str(files[0]))
    assert reopened.slides[0].shapes.title.text == "项目汇报"


async def test_xml_like_text_is_written_literally(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    raw = "<w:p><w:r>不该被解析成 XML</w:r></w:p>"
    formula = "=HYPERLINK(\"http://example.com\")"
    result = await spec_of(installed, "write_docx").callback(
        context(bot, installed),
        title="原文",
        sections=[{"paragraph": raw}, {"paragraph": formula}],
    )
    assert result["sent"] is True
    _name, payload, _content = attachment(sender)
    document = Document(io.BytesIO(payload))
    texts = [paragraph.text for paragraph in document.paragraphs]
    assert raw in texts and formula in texts
    assert not document.inline_shapes


# --------------------------------------------------------------------------- #
# 文件名
# --------------------------------------------------------------------------- #
async def test_filename_forces_extension_and_falls_back_to_title(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    callback = spec_of(installed, "write_docx").callback
    cases = [("季度报告.txt", "季度报告.docx"), ("报告.exe", "报告.docx"), ("v1.2报告", "v1.2报告.docx")]
    for provided, expected in cases:
        result = await callback(context(bot, installed), title="季度报告", sections=SECTIONS, filename=provided)
        assert result["sent"] is True and result["filename"] == expected
        assert attachment(sender)[0] == expected

    derived = await callback(context(bot, installed), title="周报：总结/结果", sections=SECTIONS)
    assert derived["sent"] is True and derived["filename"] == "周报总结结果.docx"


@pytest.mark.parametrize(
    "filename",
    ["../evil.docx", "..\\evil.docx", "C:\\Windows\\evil.docx", "/etc/evil.docx",
     ".hidden.docx", "报告\x00.docx", "报告/子目录.docx", "报告<>.docx", "报告?.docx", "CON.docx"],
)
async def test_filename_rejects_escape_hidden_and_unsafe_names(docs_bot, monkeypatch, filename) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    result = await spec_of(installed, "write_docx").callback(
        context(bot, installed), title="报告", sections=SECTIONS, filename=filename
    )
    assert result["error"] == "docs_filename_invalid"
    assert "message" in result
    sender.assert_not_awaited()
    assert data_files(installed) == []


# --------------------------------------------------------------------------- #
# 受控结构与上限
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sections",
    [
        [{"heading": "标题", "paragraph": "正文"}],
        [{"xml": "<w:p/>"}],
        [{"heading": "标题", "level": 5}],
        [{"paragraph": "正文", "bullets": ["要点"]}],
        [],
        ["不是对象"],
        [{"table": {"headers": ["A", "B"], "rows": [["1"]]}}],
    ],
)
async def test_docx_section_structure_is_restricted(docs_bot, monkeypatch, sections) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    result = await spec_of(installed, "write_docx").callback(
        context(bot, installed), title="报告", sections=sections
    )
    assert result["error"] == "docs_content_invalid"
    sender.assert_not_awaited()
    assert data_files(installed) == []


async def test_limits_are_enforced_from_settings(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    await installed.settings.update(
        {
            "max_blocks": 2,
            "max_chars": 200,
            "max_slides": 1,
            "max_rows": 5,
            "max_cols": 5,
            "max_cells": 5,
            "max_cell_chars": 10,
            "max_bullets": 2,
        }
    )
    docx = spec_of(installed, "write_docx").callback
    xlsx = spec_of(installed, "write_xlsx").callback
    pptx = spec_of(installed, "write_pptx").callback
    docx_ctx = context(bot, installed)

    blocks = await docx(docx_ctx, title="报告", sections=[{"paragraph": "一"}, {"paragraph": "二"}, {"paragraph": "三"}])
    assert blocks["error"] == "docs_limits_exceeded"
    too_long = await docx(docx_ctx, title="报告", sections=[{"paragraph": "长" * 300}])
    assert too_long["error"] == "docs_text_too_long"
    too_many_bullets = await docx(docx_ctx, title="报告", sections=[{"bullets": ["一", "二", "三"]}])
    assert too_many_bullets["error"] == "docs_limits_exceeded"

    too_many_rows = await xlsx(docx_ctx, sheet_name="表", rows=[["1"], ["2"], ["3"], ["4"], ["5"], ["6"]])
    assert too_many_rows["error"] == "docs_limits_exceeded"
    too_many_cols = await xlsx(docx_ctx, sheet_name="表", rows=[["1", "2", "3", "4", "5", "6"]])
    assert too_many_cols["error"] == "docs_limits_exceeded"
    too_many_cells = await xlsx(docx_ctx, sheet_name="表", rows=[["1", "2", "3"], ["4", "5", "6"]])
    assert too_many_cells["error"] == "docs_limits_exceeded"
    too_long_cell = await xlsx(docx_ctx, sheet_name="表", rows=[["字" * 20]])
    assert too_long_cell["error"] == "docs_cell_invalid"

    too_many_slides = await pptx(
        docx_ctx,
        title="汇报",
        slides=[{"title": "一", "bullets": []}, {"title": "二", "bullets": []}],
    )
    assert too_many_slides["error"] == "docs_limits_exceeded"
    sender.assert_not_awaited()


@pytest.mark.parametrize("cell", [{"nested": 1}, ["列表"], None, float("nan")])
async def test_excel_cell_types_are_restricted(docs_bot, monkeypatch, cell) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    result = await spec_of(installed, "write_xlsx").callback(
        context(bot, installed), sheet_name="表", rows=[["值", cell]]
    )
    assert result["error"] == "docs_cell_invalid"
    sender.assert_not_awaited()
    assert data_files(installed) == []


def test_oversized_file_is_rejected_before_writing(tmp_path: Path) -> None:
    service = sys.modules["fllbot_test_docs_plugin.service"]
    limits = service.Limits(max_file_bytes=1024)
    target = tmp_path / "files"
    with pytest.raises(service.DocsError) as caught:
        service.write_docx(target, "报告.docx", "标题", [{"paragraph": "内容"}], limits)
    assert caught.value.code == "docs_too_large"
    assert not target.exists()


# --------------------------------------------------------------------------- #
# 沙盒、授权、发送与清理
# --------------------------------------------------------------------------- #
async def test_automatic_participation_is_blocked_by_sandbox(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    await bot.permissions.set_plugin_enabled("docs", True)
    spec = spec_of(installed, "write_docx")
    registry = {spec.qualified_name: (installed, spec)}
    arguments = {"title": "报告", "sections": [{"paragraph": "内容"}], "filename": "报告.docx"}

    automatic = context(bot, installed)
    automatic.trigger_decision = SimpleNamespace(trigger_kind="automatic")
    automatic.tool_call_id = "automatic-call"
    output, _ = await bot.tool_executor.execute(automatic, spec.qualified_name, arguments, registry)
    blocked = json.loads(output)
    assert blocked["error"] == "tool_blocked"
    assert blocked["reason"] == "automatic_side_effect"
    sender.assert_not_awaited()
    assert data_files(installed) == []

    explicit = context(bot, installed)
    explicit.trigger_decision = SimpleNamespace(trigger_kind="at")
    explicit.tool_call_id = "explicit-call"
    output, _ = await bot.tool_executor.execute(explicit, spec.qualified_name, arguments, registry)
    allowed = json.loads(output)
    assert allowed["sent"] is True and allowed["filename"] == "报告.docx"
    sender.assert_awaited_once()


async def test_tools_are_invisible_until_authorized(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    ctx = context(bot, installed)
    definitions = {"allowed_tools": [], "trigger_modes": ["at"], "trigger_prefix": ""}
    monkeypatch.setattr(bot.chat, "turn_agent_settings", lambda *args, **kwargs: definitions)
    _defs, registry = _get_tools(ctx)
    assert not [name for name in registry if name.startswith("docs__")]

    definitions = {
        "allowed_tools": ["docs__write_xlsx"],
        "trigger_modes": ["at"],
        "trigger_prefix": "",
    }
    _defs, registry = _get_tools(ctx)
    assert "docs__write_xlsx" in registry
    assert "docs__write_docx" not in registry and "docs__write_pptx" not in registry


@pytest.mark.parametrize("failure", ["false", "exception"])
async def test_send_failure_reports_error_and_removes_file(docs_bot, monkeypatch, failure) -> None:
    bot, installed = docs_bot
    if failure == "false":
        sender = capture_send(bot, monkeypatch, result=False)
    else:
        sender = AsyncMock(side_effect=RuntimeError("send broken"))
        monkeypatch.setattr(bot.deliveries, "send_media", sender)
    other = installed.data_dir / "files" / "0123456789ab_other.docx"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"other request")
    result = await spec_of(installed, "write_docx").callback(
        context(bot, installed), title="报告", sections=SECTIONS
    )
    assert result["sent"] is False
    assert result["error"] == "docs_not_delivered"
    assert "送达" in result["message"]
    sender.assert_awaited_once()
    assert other.exists()
    assert data_files(installed) == [other]


async def test_missing_dependency_returns_readable_error(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    module = sys.modules[installed.module.__name__ + ".service"]

    def missing() -> None:
        raise module.DocsError("docs_dependency_missing", "缺少 python-docx，无法生成 Word 文档。")

    monkeypatch.setattr(module, "_docx", missing)
    result = await spec_of(installed, "write_docx").callback(
        context(bot, installed), title="报告", sections=SECTIONS
    )
    assert result["error"] == "docs_dependency_missing"
    assert "python-docx" in result["message"]
    sender.assert_not_awaited()
    assert data_files(installed) == []


async def test_generation_failure_keeps_other_files(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    module = sys.modules[installed.module.__name__ + ".service"]
    other = installed.data_dir / "files" / "0123456789ab_other.docx"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"other request")

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("generation broken")

    monkeypatch.setattr(module, "build_docx", broken)
    result = await spec_of(installed, "write_docx").callback(
        context(bot, installed), title="报告", sections=SECTIONS
    )
    assert result["error"] == "docs_generate_failed"
    sender.assert_not_awaited()
    assert other.exists()
    assert data_files(installed) == [other]


async def test_validation_failure_removes_only_current_file(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    module = sys.modules[installed.module.__name__ + ".service"]
    other = installed.data_dir / "files" / "0123456789ab_other.docx"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"other request")

    def invalid(path: Path, title: str) -> None:
        raise module.DocsError("docs_generate_failed", "生成的文件无法通过重新打开校验，未发送。")

    monkeypatch.setattr(module, "validate_docx", invalid)
    result = await spec_of(installed, "write_docx").callback(
        context(bot, installed), title="报告", sections=SECTIONS
    )
    assert result["error"] == "docs_generate_failed"
    sender.assert_not_awaited()
    assert other.exists()
    assert data_files(installed) == [other]


async def test_old_files_are_cleaned_after_successful_send(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    directory = installed.data_dir / "files"
    directory.mkdir(parents=True)
    stale = time.time() - 10 * 86_400
    old = directory / "0123456789ab_old.docx"
    old.write_bytes(b"old")
    os.utime(old, (stale, stale))
    manual = directory / "manual.docx"
    manual.write_bytes(b"not generated by this plugin")
    os.utime(manual, (stale, stale))
    recent = directory / "fedcba987654_recent.docx"
    recent.write_bytes(b"recent")

    result = await spec_of(installed, "write_docx").callback(
        context(bot, installed), title="新报告", sections=SECTIONS
    )
    assert result["sent"] is True
    sender.assert_awaited_once()
    assert not old.exists()
    assert manual.exists()
    assert recent.exists()
    assert any(path.name.endswith("_新报告.docx") for path in directory.glob("*"))


async def test_on_load_and_periodic_cleanup_remove_only_generated_files(docs_bot) -> None:
    bot, installed = docs_bot
    directory = installed.data_dir / "files"
    directory.mkdir(parents=True)
    stale = time.time() - 10 * 86_400
    first = directory / "0123456789ab_first.docx"
    first.write_bytes(b"old")
    os.utime(first, (stale, stale))
    manual = directory / "manual.docx"
    manual.write_bytes(b"keep me")
    os.utime(manual, (stale, stale))
    recent = directory / "fedcba987654_recent.docx"
    recent.write_bytes(b"recent")

    # 加载时清理一次
    await installed.on_load(bot)
    assert not first.exists()
    assert manual.exists() and recent.exists()

    # 周期任务按 retention_days 清理旧文件，同样只认本插件前缀
    second = directory / "abcdef012345_second.docx"
    second.write_bytes(b"old")
    os.utime(second, (stale, stale))
    await docs_plugin.cleanup_generated_files(bot)
    assert not second.exists()
    assert manual.exists() and recent.exists()


def test_package_declares_periodic_cleanup() -> None:
    loops = {loop.name: loop for loop in docs_plugin.plugin.loops}
    assert "cleanup_files" in loops
    loop = loops["cleanup_files"]
    assert loop.seconds == 6 * 60 * 60
    assert 0 < loop.initial_delay <= 60 * 60
    assert docs_plugin.CLEANUP_INTERVAL_SECONDS == loop.seconds
    assert callable(docs_plugin.cleanup_generated_files)


def test_default_file_limit_is_five_megabytes(docs_bot) -> None:
    _bot, installed = docs_bot
    assert installed.settings.get("max_file_mb") == 5.0
    schema = {field["key"]: field for field in installed.settings.schema()}
    assert schema["max_file_mb"]["default"] == 5.0
    service = sys.modules[installed.module.__name__ + ".service"]
    assert service.Limits().max_file_bytes == 5 * 1024 * 1024


async def test_automatic_participation_callback_returns_addressed_required(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    spec = spec_of(installed, "write_xlsx").callback
    arguments = {"sheet_name": "表", "rows": [["值"]]}

    automatic = context(bot, installed, "整理一下")
    automatic.trigger_decision = SimpleNamespace(trigger_kind="automatic", addressed=False)
    result = await spec(automatic, **arguments)
    assert result["error"] == "docs_addressed_required"
    assert "明确请求" in result["message"]
    sender.assert_not_awaited()
    assert data_files(installed) == []

    explicit = context(bot, installed, "做个表格发我", at=10001)
    explicit.trigger_decision = SimpleNamespace(trigger_kind="automatic", addressed=True)
    result = await spec(explicit, **arguments)
    assert result["sent"] is True
    sender.assert_awaited_once()


async def test_cleanup_failure_is_logged_not_raised(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    module = sys.modules[installed.module.__name__ + ".service"]

    def broken(*args: Any, **kwargs: Any) -> int:
        raise OSError("disk unavailable")

    monkeypatch.setattr(module, "cleanup", broken)
    # on_load 与周期任务都必须吞掉清理异常，只记日志。
    await installed.on_load(bot)
    await docs_plugin.cleanup_generated_files(bot)


async def test_automatic_participation_rejected_when_sandbox_disabled(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    await bot.update_config({"sandbox": {"enabled": False}})
    await bot.permissions.set_plugin_enabled("docs", True)
    spec = spec_of(installed, "write_docx")
    registry = {spec.qualified_name: (installed, spec)}
    automatic = context(bot, installed)
    automatic.trigger_decision = SimpleNamespace(trigger_kind="automatic", addressed=False)
    automatic.tool_call_id = "sandbox-disabled-call"
    output, _ = await bot.tool_executor.execute(
        automatic,
        spec.qualified_name,
        {"title": "报告", "sections": [{"paragraph": "内容"}]},
        registry,
    )
    payload = json.loads(output)
    assert payload["error"] == "docs_addressed_required"
    assert payload["message"]
    sender.assert_not_awaited()
    assert data_files(installed) == []


async def test_disabled_plugin_returns_readable_error(docs_bot, monkeypatch) -> None:
    bot, installed = docs_bot
    sender = capture_send(bot, monkeypatch)
    await installed.settings.update({"enabled": False})
    result = await spec_of(installed, "write_pptx").callback(
        context(bot, installed), title="汇报", slides=[{"title": "一", "bullets": []}]
    )
    assert result["error"] == "docs_disabled"
    sender.assert_not_awaited()
    assert data_files(installed) == []
