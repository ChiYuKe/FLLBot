"""GitHub 插件：URL 校验、缓存、目录/源码/版本/提交读取与错误分类。"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from fllbot.builtin_plugins.github import service
from fllbot.builtin_plugins.github.service import (
    GitHubError,
    list_directory,
    parse_repository,
    read_commits,
    read_releases,
    read_repository,
    read_source_files,
    search_repositories,
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _metadata(**overrides) -> dict:
    data = {
        "private": False,
        "default_branch": "main",
        "html_url": "https://github.com/owner/repo",
        "full_name": "owner/repo",
        "description": "demo",
        "stargazers_count": 2,
        "forks_count": 1,
        "open_issues_count": 0,
        "language": "C#",
        "topics": ["oni"],
        "license": {"spdx_id": "MIT"},
        "pushed_at": "2026-01-02T03:04:05Z",
        "created_at": "2025-01-01T00:00:00Z",
        "archived": False,
        "homepage": "",
    }
    data.update(overrides)
    return data


def _blob(text: str, path: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "path": path,
            "type": "file",
            "encoding": "base64",
            "content": _b64(text),
            "size": len(text.encode()),
        },
    )


def _readme(text: str) -> httpx.Response:
    return httpx.Response(
        200, json={"path": "README.md", "encoding": "base64", "content": _b64(text), "size": len(text)}
    )


def _tree(*paths: str, truncated: bool = False) -> httpx.Response:
    return httpx.Response(
        200,
        json={"tree": [{"path": path, "type": "blob", "size": 10} for path in paths], "truncated": truncated},
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    service._CACHE.clear()
    service._RATE.clear()
    yield
    service._CACHE.clear()
    service._RATE.clear()


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    original = httpx.AsyncClient
    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def _handler(routes: dict, calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        response = routes.get(request.url.path)
        if response is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return response

    return handler


def test_repository_url_validation():
    assert parse_repository("https://github.com/ChiYuKe/ONI-Mods") == ("ChiYuKe", "ONI-Mods")
    assert parse_repository("https://www.github.com/ChiYuKe/ONI-Mods/tree/master/README.md") == (
        "ChiYuKe",
        "ONI-Mods",
    )
    assert parse_repository("github.com/ChiYuKe/ONI-Mods/") == ("ChiYuKe", "ONI-Mods")
    assert parse_repository("git@github.com:ChiYuKe/ONI-Mods.git") == ("ChiYuKe", "ONI-Mods")
    for url in (
        "https://github.com.evil.invalid/owner/repo",
        "http://github.com/owner/repo",
        "https://u:p@github.com/owner/repo",
        "https://github.com/owner",
        "https://github.com/owner/../repo",
        "",
    ):
        with pytest.raises(GitHubError):
            parse_repository(url)


async def test_read_repository_returns_readme_tree_and_images(monkeypatch):
    calls: list[str] = []
    routes = {
        "/repos/owner/repo": httpx.Response(200, json=_metadata()),
        "/repos/owner/repo/readme": _readme("# 示例仓库\n\n<img src=x>"),
        "/repos/owner/repo/git/trees/main": _tree(
            "README.md",
            "LICENSE",
            "assets/demo.png",
            "docs/guide.md",
            "node_modules/lib/index.js",
            "src/Mod.cs",
            "src/sub/Deep.cs",
        ),
        "/repos/owner/repo/contents/docs/guide.md": _blob("# Guide", "docs/guide.md"),
    }
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_repository("https://github.com/owner/repo", paths=["docs/guide.md"])

    assert data["readme"] == "# 示例仓库\n\n<img src=x>"
    assert data["images"] == ["assets/demo.png"]
    assert data["files"] == [
        "LICENSE",
        "README.md",
        "src/Mod.cs",
        "src/sub/Deep.cs",
        "docs/guide.md",
    ]
    assert data["file_count"] == 5
    assert "node_modules/lib/index.js" not in data["files"]
    assert data["selected_files"]["docs/guide.md"]["content"] == "# Guide"
    assert data["description"] == "demo"
    assert data["topics"] == ["oni"]
    assert data["license"] == "MIT"
    assert len(calls) == 4

    scoped = await read_repository("https://github.com/owner/repo", tree_path="src", offset=1, limit=20)
    assert scoped["files"] == ["src/sub/Deep.cs"]
    assert scoped["file_count"] == 2
    assert scoped["next_offset"] is None
    # 缓存命中：第二次读取不应再产生 API 请求。
    assert len(calls) == 4


async def test_read_repository_without_readme_still_works(monkeypatch):
    calls: list[str] = []
    routes = {
        "/repos/owner/noreadme": httpx.Response(
            200, json=_metadata(full_name="owner/noreadme", html_url="https://github.com/owner/noreadme")
        ),
        "/repos/owner/noreadme/git/trees/main": _tree("Program.cs"),
    }
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_repository("https://github.com/owner/noreadme")

    assert data["readme"] == ""
    assert data["readme_missing"] is True
    assert data["files"] == ["Program.cs"]


async def test_read_repository_resolves_ref_with_slash(monkeypatch):
    calls: list[str] = []
    routes = {
        "/repos/owner/slashy": httpx.Response(
            200, json=_metadata(full_name="owner/slashy", html_url="https://github.com/owner/slashy")
        ),
        "/repos/owner/slashy/readme": _readme("hello"),
        "/repos/owner/slashy/git/trees/feature/x": httpx.Response(404, json={"message": "Not Found"}),
        "/repos/owner/slashy/commits": httpx.Response(200, json=[{"sha": "abc123"}]),
        "/repos/owner/slashy/git/trees/abc123": _tree("src/Feature.cs"),
    }
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_repository("https://github.com/owner/slashy", ref="feature/x")

    assert data["default_branch"] == "feature/x"
    assert data["files"] == ["src/Feature.cs"]
    assert any("/git/trees/abc123" in url for url in calls)


async def test_repository_output_stays_within_budget(monkeypatch):
    long_readme = "x" * 12000
    paths = [f"src/very/deep/folder/{index:03d}_module_with_a_long_name.cs" for index in range(300)]
    routes = {
        "/repos/owner/huge": httpx.Response(
            200, json=_metadata(full_name="owner/huge", html_url="https://github.com/owner/huge")
        ),
        "/repos/owner/huge/readme": _readme(long_readme),
        "/repos/owner/huge/git/trees/main": _tree(*paths),
    }
    for path in ("a.cs", "b.cs", "c.cs", "d.cs"):
        routes[f"/repos/owner/huge/contents/{path}"] = _blob("y" * 8000, path)
    calls: list[str] = []
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_repository("https://github.com/owner/huge", paths=["a.cs", "b.cs", "c.cs", "d.cs"])

    assert len(json.dumps(data, ensure_ascii=False)) <= 20000
    assert data["file_count"] == 300


async def test_read_repository_tree_view(monkeypatch):
    calls: list[str] = []
    routes = {
        "/repos/owner/tree": httpx.Response(
            200, json=_metadata(full_name="owner/tree", html_url="https://github.com/owner/tree")
        ),
        "/repos/owner/tree/git/trees/main": _tree(
            "README.md",
            "assets/demo.png",
            "docs/guide.md",
            "src/Mod.cs",
            "src/sub/Deep.cs",
        ),
    }
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_repository("https://github.com/owner/tree", view="tree")

    assert data["view"] == "tree"
    assert data["tree"].splitlines() == [
        "tree/",
        "├── assets/",
        "│   └── demo.png",
        "├── docs/",
        "│   └── guide.md",
        "├── src/",
        "│   ├── sub/",
        "│   └── Mod.cs",
        "└── README.md",
    ]
    assert data["paths"] == ["docs/guide.md", "src/Mod.cs", "README.md"]
    assert data["file_count"] == 5
    assert data["depth"] == 2
    assert data["tree_truncated"] is False
    assert data["has_more_depth"] is True
    assert len(calls) == 2  # 结构树视图不读取 README

    scoped = await read_repository("https://github.com/owner/tree", view="tree", tree_path="src")
    assert scoped["tree"].splitlines() == [
        "tree/src/",
        "├── sub/",
        "│   └── Deep.cs",
        "└── Mod.cs",
    ]
    assert scoped["paths"] == ["src/sub/Deep.cs", "src/Mod.cs"]
    assert scoped["file_count"] == 2
    assert len(calls) == 2  # 第二轮命中缓存


async def test_tree_view_falls_back_to_top_level(monkeypatch):
    paths = [f"mod{index:03d}/{name}.cs" for index in range(120) for name in ("a", "b")]
    routes = {
        "/repos/owner/wide": httpx.Response(
            200, json=_metadata(full_name="owner/wide", html_url="https://github.com/owner/wide")
        ),
        "/repos/owner/wide/git/trees/main": _tree(*paths),
    }
    calls: list[str] = []
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_repository("https://github.com/owner/wide", view="tree")

    assert data["depth"] == 1
    assert data["depth_reduced"] is True
    assert data["tree_truncated"] is False
    assert data["paths"] == []
    assert data["file_count"] == 240
    assert "a.cs" not in data["tree"]


async def test_tree_view_stays_within_budget(monkeypatch):
    paths = [f"dir{index:03d}/{'x' * 60}{index}.cs" for index in range(200)]
    routes = {
        "/repos/owner/deep": httpx.Response(
            200, json=_metadata(full_name="owner/deep", html_url="https://github.com/owner/deep")
        ),
        "/repos/owner/deep/git/trees/main": _tree(*paths),
    }
    _patch_transport(monkeypatch, _handler(routes, []))

    data = await read_repository("https://github.com/owner/deep", view="tree")

    assert len(json.dumps(data, ensure_ascii=False)) <= 20000
    assert data["tree_truncated"] is True


async def test_tree_view_rejects_unknown_view():
    with pytest.raises(GitHubError, match="view"):
        await read_repository("https://github.com/owner/repo", view="graph")


async def test_read_sources_supports_line_ranges_and_next_line(monkeypatch):
    text = "\n".join(f"line{number}" for number in range(1, 6))
    routes = {
        "/repos/owner/sources": httpx.Response(200, json=_metadata()),
        "/repos/owner/sources/contents/src/Mod.cs": _blob(text, "src/Mod.cs"),
        "/repos/owner/sources/contents/src/Bin.cs": _blob("a\x00b", "src/Bin.cs"),
    }
    calls: list[str] = []
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_source_files(
        "https://github.com/owner/sources", ["src/Mod.cs#L2-L3", "../bad", "src/Bin.cs"]
    )

    assert data["ref"] == "main"
    selected = data["source_files"]["src/Mod.cs"]
    assert selected["content"] == "line2\nline3"
    assert (selected["start_line"], selected["end_line"], selected["total_lines"]) == (2, 3, 5)
    assert selected["next_line"] == 4
    assert ".." in data["source_files"]["../bad"]["error"]
    assert "二进制" in data["source_files"]["src/Bin.cs"]["error"]


async def test_read_sources_rejects_empty_selection():
    with pytest.raises(GitHubError, match="选择"):
        await read_source_files("https://github.com/owner/repo", [])


async def test_read_sources_fits_output_budget(monkeypatch):
    routes = {"/repos/owner/wide": httpx.Response(200, json=_metadata())}
    names = [f"src/File{index}.cs" for index in range(8)]
    for name in names:
        routes[f"/repos/owner/wide/contents/{name}"] = _blob("z" * 9000, name)
    _patch_transport(monkeypatch, _handler(routes, []))

    data = await read_source_files("https://github.com/owner/wide", names)

    assert len(json.dumps(data, ensure_ascii=False)) <= 30000
    skipped = [value for value in data["source_files"].values() if value.get("skipped") == "output_budget"]
    assert skipped
    assert all(value["next_line"] == 1 for value in skipped)


async def test_list_directory_pages_entries(monkeypatch):
    routes = {
        "/repos/owner/tree": httpx.Response(
            200, json=_metadata(full_name="owner/tree", html_url="https://github.com/owner/tree")
        ),
        "/repos/owner/tree/contents/src": httpx.Response(
            200,
            json=[
                {"name": "A.cs", "path": "src/A.cs", "type": "file", "size": 12},
                {"name": "sub", "path": "src/sub", "type": "dir", "size": 0},
            ],
        ),
    }
    _patch_transport(monkeypatch, _handler(routes, []))

    data = await list_directory("https://github.com/owner/tree", path="src", limit=1)

    assert data["total"] == 2
    assert data["entries"] == [{"name": "A.cs", "path": "src/A.cs", "type": "file", "size": 12}]
    assert data["next_offset"] == 1


async def test_search_repositories_returns_compact_results(monkeypatch):
    routes = {
        "/search/repositories": httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [
                    {
                        "full_name": "owner/mod",
                        "html_url": "https://github.com/owner/mod",
                        "description": "ONI 模组",
                        "language": "C#",
                        "stargazers_count": 12,
                        "forks_count": 3,
                        "pushed_at": "2026-02-03T00:00:00Z",
                        "topics": ["oni", "mod"],
                        "archived": False,
                        "license": {"spdx_id": "MIT"},
                    }
                ],
            },
        )
    }
    calls: list[str] = []
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await search_repositories("topic:oni language:C#")

    assert data["total_count"] == 1
    assert data["results"][0]["full_name"] == "owner/mod"
    assert data["results"][0]["stars"] == 12
    assert "q=topic%3Aoni+language%3AC%23" in calls[0]


async def test_read_releases_and_tag_fallback(monkeypatch):
    routes = {
        "/repos/owner/rel": httpx.Response(
            200, json=_metadata(full_name="owner/rel", html_url="https://github.com/owner/rel")
        ),
        "/repos/owner/rel/releases": httpx.Response(
            200,
            json=[
                {
                    "tag_name": "v1.2.0",
                    "name": "春季更新",
                    "published_at": "2026-03-01T00:00:00Z",
                    "prerelease": False,
                    "html_url": "https://github.com/owner/rel/releases/tag/v1.2.0",
                    "body": "修复崩溃",
                    "assets": [{"name": "mod.zip", "size": 1024}],
                }
            ],
        ),
        "/repos/owner/rel2": httpx.Response(
            200, json=_metadata(full_name="owner/rel2", html_url="https://github.com/owner/rel2")
        ),
        "/repos/owner/rel2/releases": httpx.Response(200, json=[]),
        "/repos/owner/rel2/tags": httpx.Response(200, json=[{"name": "v1.0", "commit": {"sha": "abcdef123456"}}]),
    }
    _patch_transport(monkeypatch, _handler(routes, []))

    released = await read_releases("https://github.com/owner/rel")
    assert released["releases"][0]["tag"] == "v1.2.0"
    assert released["releases"][0]["assets"] == [{"name": "mod.zip", "size": 1024}]

    tagged = await read_releases("https://github.com/owner/rel2")
    assert tagged["releases"] == []
    assert tagged["tags"] == [{"tag": "v1.0", "commit": "abcdef1"}]


async def test_read_commits_returns_recent_history(monkeypatch):
    routes = {
        "/repos/owner/commits": httpx.Response(200, json=_metadata()),
        "/repos/owner/commits/commits": httpx.Response(
            200,
            json=[
                {
                    "sha": "1234567890abcdef",
                    "html_url": "https://github.com/owner/commits/commit/1234567",
                    "commit": {
                        "message": "修复繁殖逻辑\n\n详细说明",
                        "author": {"name": "Alice", "date": "2026-04-01T10:00:00Z"},
                    },
                }
            ],
        ),
    }
    calls: list[str] = []
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_commits("https://github.com/owner/commits", path="src/Mod.cs", limit=5)

    assert data["path"] == "src/Mod.cs"
    assert data["commits"][0] == {
        "sha": "1234567",
        "message": "修复繁殖逻辑",
        "author": "Alice",
        "date": "2026-04-01T10:00:00Z",
        "url": "https://github.com/owner/commits/commit/1234567",
    }
    assert "path=src%2FMod.cs" in calls[-1]


async def test_github_errors_are_specific():
    class Client:
        def __init__(self, response):
            self.response = response

        async def get(self, *args, **kwargs):
            return self.response

    cases = (
        (httpx.Response(401), "令牌"),
        (httpx.Response(404), "不存在"),
        (httpx.Response(403, headers={"x-ratelimit-remaining": "0"}), "速率上限"),
        (httpx.Response(403), "拒绝"),
        (httpx.Response(429), "频率"),
        (httpx.Response(451), "法律"),
        (httpx.Response(500), "暂时不可用"),
    )
    for response, expected in cases:
        with pytest.raises(service.GitHubError, match=expected):
            await service._get(Client(response), "https://api.github.com/repos/owner/repo", "")


def test_builtin_manifest_and_agent_tools():
    from fllbot.builtin_plugins.github import plugin

    assert not plugin.handlers
    assert [tool.name for tool in plugin.ai_tools] == [
        "read_repository",
        "list_directory",
        "read_sources",
        "search_repositories",
        "read_releases",
        "read_commits",
    ]
    assert {setting.key for setting in plugin.settings.fields.values()} >= {"token", "timeout", "cache_seconds"}
    assert plugin.agent_instructions[0].name == "repository_analysis"
    assert plugin.agent_instructions[0].tools == ("read_repository",)
    properties = plugin.ai_tools[0].parameters["properties"]
    assert {"repository", "view", "depth", "tree_path"} <= set(properties)


async def test_plugin_tool_callback_wraps_errors(monkeypatch):
    from types import SimpleNamespace

    from fllbot.builtin_plugins.github import plugin

    calls: list[str] = []
    _patch_transport(monkeypatch, _handler({}, calls))
    ctx = SimpleNamespace(settings=SimpleNamespace(get=lambda key, default=None: None))

    result = await plugin.ai_tools[0].callback(ctx, "https://github.com/owner")

    assert "error" in result
    assert result["repository"] == "https://github.com/owner"
