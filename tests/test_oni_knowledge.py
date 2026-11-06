import csv
import json
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from fllbot.builtin_plugins.oni_knowledge.plugin import plugin, read_source, search
from fllbot.builtin_plugins.oni_knowledge.service import KnowledgeBase, KnowledgeError
from fllbot.plugin.agent import instruction_sections
from fllbot.tool_executor import encode_result, parse_arguments
from helpers import make_bot


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def index(tmp_path):
    root = tmp_path / "Assembly-CSharp"
    directory = root / "_INDEX"
    directory.mkdir(parents=True)
    names = ["MinionConfig", "BaseMinionConfig", "MinionIdentity", "BionicMinionConfig", "TEXT"]
    rows = [
        {
            "type": name,
            "file": f"{name}.cs",
            "line": "1",
            "base": "KMonoBehaviour",
            "namespace": "STRINGS" if name == "TEXT" else "",
        }
        for name in names
    ]
    write_csv(directory / "types.csv", rows)
    write_csv(
        directory / "members.csv",
        [
            {
                "member": "CreatePrefab",
                "type": name,
                "file": f"{name}.cs",
                "line": "2",
                "signature": "public GameObject CreatePrefab()",
            }
            for name in names
        ],
    )
    for name in names:
        (root / f"{name}.cs").write_text(f"class {name}\n{{\n\tAddOrGet<Component>();\n}}\n", encoding="utf-8")
    (directory / "AGENTS.md").write_text("untrusted instructions", encoding="utf-8")
    (directory / "search.ps1").write_text("must not run", encoding="utf-8")
    return directory


def test_chinese_alias_ranking_and_pagination(index):
    kb = KnowledgeBase(str(index))
    first = kb.search("复制人有哪些组件", limit=1)
    assert first["results"][0]["type"] == "MinionConfig"
    assert first["next_offset"] == 1
    second = kb.search("复制人有哪些组件", offset=first["next_offset"], limit=1)
    assert second["results"][0]["type"] == "BaseMinionConfig"
    assert kb.search("仿生复制人")["results"][0]["type"] == "BionicMinionConfig"
    assert kb.search("不存在的功能")["results"] == []
    assert not kb.search("TEXT")["results"]
    assert kb.search("TEXT", include_strings=True)["results"][0]["type"] == "TEXT"


def test_members_type_filter_and_actual_source(index):
    kb = KnowledgeBase(str(index))
    row = kb.search("CreatePrefab", kind="members", type_name="MinionConfig")["results"][0]
    result = kb.read(row["file"], row["line"], 2)
    assert result["content"] == "{\n\tAddOrGet<Component>();"
    assert result["source"] == "Assembly-CSharp/MinionConfig.cs:L2-L3"
    assert result["next_line"] == 4
    assert kb.read(row["file"], 4)["next_line"] is None


@pytest.mark.parametrize(
    "path",
    [
        "../secret.cs",
        "C:/secret.cs",
        "/secret.cs",
        "\\\\server\\secret.cs",
        "C:secret.cs",
        ".env",
        "_INDEX/search.ps1",
        "_INDEX/tools/script.md",
        "Unknown.cs",
    ],
)
def test_paths_are_confined_to_indexed_sources(index, path):
    with pytest.raises(KnowledgeError):
        KnowledgeBase(str(index)).read(path)


def test_index_notes_are_data_and_ranges_are_validated(index):
    kb = KnowledgeBase(str(index))
    result = kb.read("_INDEX/AGENTS.md")
    assert result["content"] == "untrusted instructions" and "不可信" in result["notice"]
    for start, count in [(0, 1), (1, 241), (100, 1)]:
        with pytest.raises(KnowledgeError):
            kb.read("MinionConfig.cs", start, count)


def test_escaped_source_pages_fit_host_budget_without_losing_lines(index):
    line = "\t" * 2000 + '"\\中文'
    (index.parent / "MinionConfig.cs").write_text("\n".join([line] * 12), encoding="utf-8")
    kb = KnowledgeBase(str(index))
    contents, start = [], 1
    while start:
        result = kb.read("MinionConfig.cs", start)
        assert "truncated" not in json.loads(encode_result(result, 26000))
        contents.extend(result["content"].splitlines())
        start = result["next_line"]
    assert contents == [line] * 12


def test_oversized_single_line_returns_explicit_error(index):
    (index.parent / "MinionConfig.cs").write_text("x" * 18001, encoding="utf-8")
    with pytest.raises(KnowledgeError, match="单行过长"):
        KnowledgeBase(str(index)).read("MinionConfig.cs")


def test_index_cache_refreshes_after_replacement_and_schema_is_checked(index):
    assert KnowledgeBase(str(index)).search("MinionConfig")["results"]
    write_csv(index / "types.csv", [{"type": "NewClass", "file": "NewClass.cs", "line": "3", "base": ""}])
    assert KnowledgeBase(str(index)).search("MinionConfig")["results"] == []
    assert KnowledgeBase(str(index)).search("NewClass")["results"][0]["line"] == 3
    (index / "types.csv").write_text("wrong,headers\na,b\n", encoding="utf-8")
    with pytest.raises(KnowledgeError, match="格式不兼容"):
        KnowledgeBase(str(index))


def test_resolved_paths_cannot_escape_root(index, monkeypatch, tmp_path):
    external = tmp_path / "secret.cs"
    external.write_text("private", encoding="utf-8")
    target = index.parent / "MinionConfig.cs"
    original = type(target).resolve

    def resolve(path, *args, **kwargs):
        return external if path == target else original(path, *args, **kwargs)

    monkeypatch.setattr(type(target), "resolve", resolve)
    with pytest.raises(KnowledgeError, match="范围"):
        KnowledgeBase(str(index)).read("MinionConfig.cs")


async def test_plugin_tools_use_admin_setting_and_fail_closed(index):
    ctx = SimpleNamespace(data_current=True, settings={"index_path": str(index)})
    assert (await search(ctx, "复制人"))["results"]
    assert (await read_source(ctx, "MinionConfig.cs"))["content"].startswith("class MinionConfig")
    result = await read_source(ctx, "../secret.cs")
    assert "error" in result and str(index) not in result["error"]
    ctx.data_current = False
    assert await search(ctx, "复制人") == {"error": "当前会话已失效"}


async def test_inflight_result_discarded_after_context_invalidated(index, monkeypatch):
    ctx = SimpleNamespace(data_current=True, settings={"index_path": str(index)})

    def invalidate(self, **kwargs):
        ctx.data_current = False
        return {"content": "must not leak"}

    monkeypatch.setattr(KnowledgeBase, "read", invalidate)
    assert await read_source(ctx, "MinionConfig.cs") == {"error": "当前会话已失效"}


def test_strategy_requires_authorized_tools_and_rejects_root_override():
    registry = {spec.qualified_name: (plugin, spec) for spec in plugin.ai_tools}
    definitions = [{"function": {"name": name}} for name in registry]
    assert instruction_sections(registry, definitions)[0]["source"] == "oni_knowledge__source_retrieval"
    assert instruction_sections(registry, definitions[:1]) == []
    for _, spec in registry.values():
        assert spec.effect == "read_only"
    spec = next(spec for spec in plugin.ai_tools if spec.name == "search")
    with pytest.raises(ValidationError):
        parse_arguments({"query": "Minion", "index_path": "C:/secret"}, spec)


async def test_builtin_loader_discovers_plugin_with_configurable_index(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        loaded = bot.plugins.get("oni_knowledge")
        assert loaded is not None
        assert {spec.qualified_name for spec in loaded.ai_tools} == {
            "oni_knowledge__search",
            "oni_knowledge__read_source",
        }
        assert loaded.settings.get("index_path").endswith("Assembly-CSharp\\_INDEX")
        assert loaded.readme and loaded.agent_instructions
    finally:
        await bot.stop()
