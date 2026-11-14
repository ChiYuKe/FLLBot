"""GitHub 插件：URL 校验、缓存、目录/源码/版本/提交读取与错误分类。"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from fllbot.builtin_plugins.github import service
from fllbot.builtin_plugins.github.service import (
    GitHubError,
    list_directory,
    parse_repository,
    read_branches,
    read_commit,
    read_commits,
    read_contributors,
    read_issue,
    read_issues,
    read_pull_requests,
    read_releases,
    read_repository,
    read_source_files,
    search_code,
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
    service._AVATAR_CACHE.clear()
    yield
    service._CACHE.clear()
    service._RATE.clear()
    service._AVATAR_CACHE.clear()


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


async def test_search_code_requires_token():
    with pytest.raises(GitHubError, match="令牌"):
        await search_code("https://github.com/owner/repo", "Reproduce")
    with pytest.raises(GitHubError, match="2–200"):
        await search_code("https://github.com/owner/repo", "x", token="ghp_demo")


async def test_search_code_scopes_repository_and_returns_fragments(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(200, json=_metadata())
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [
                    {
                        "path": "src/Mod.cs",
                        "html_url": "https://github.com/owner/repo/blob/main/src/Mod.cs",
                        "text_matches": [{"fragment": "private void Reproduce()"}],
                    }
                ],
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )

    data = await search_code(
        "https://github.com/owner/repo", "Reproduce repo:other/repo", token="ghp_demo"
    )

    assert data["query"] == "Reproduce"
    assert data["total_count"] == 1
    assert data["results"][0]["path"] == "src/Mod.cs"
    assert data["results"][0]["matches"] == ["private void Reproduce()"]
    assert requests[0].url.path == "/repos/owner/repo"
    url = str(requests[-1].url)
    assert url.startswith("https://api.github.com/search/code")
    assert "repo%3Aowner%2Frepo" in url
    assert "repo%3Aother%2Frepo" not in url
    assert requests[-1].headers["accept"] == "application/vnd.github.text-match+json"


async def test_fetch_avatars_filters_hosts_and_caches(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"png-bytes", headers={"content-type": "image/png"})

    _patch_transport(monkeypatch, handler)
    urls = [
        "https://avatars.githubusercontent.com/u/1?v=4",
        "https://evil.example.com/u/2.png",
        "http://avatars.githubusercontent.com/u/3",
    ]

    first = await service.fetch_avatars(urls)

    assert first == {"https://avatars.githubusercontent.com/u/1?v=4": b"png-bytes"}
    assert len(calls) == 1
    assert "s=96" in calls[0]
    # 第二次命中进程内缓存，不再发起请求。
    assert await service.fetch_avatars(urls) == first
    assert len(calls) == 1


async def test_fetch_avatars_skips_wrong_type_and_oversized(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/u/1" in str(request.url):
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        return httpx.Response(
            200, content=b"x" * 600_000, headers={"content-type": "image/png"}
        )

    _patch_transport(monkeypatch, handler)

    assert await service.fetch_avatars(
        [
            "https://avatars.githubusercontent.com/u/1?v=4",
            "https://avatars.githubusercontent.com/u/2?v=4",
        ]
    ) == {}


async def test_fetch_avatars_rejects_credentials_and_ports():
    assert (
        await service.fetch_avatars(
            [
                "https://u:p@avatars.githubusercontent.com/u/1",
                "https://avatars.githubusercontent.com:8443/u/1",
            ]
        )
        == {}
    )


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


async def test_read_contributors_returns_ranked_authors(monkeypatch):
    routes = {
        "/repos/owner/people": httpx.Response(200, json=_metadata(full_name="owner/people")),
        "/repos/owner/people/contributors": httpx.Response(
            200,
            json=[
                {
                    "login": "CatPlasma",
                    "contributions": 120,
                    "html_url": "https://github.com/CatPlasma",
                    "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4",
                    "type": "User",
                },
                {
                    "login": None,
                    "name": "匿名提交者",
                    "email": "anon@example.com",
                    "contributions": 3,
                    "type": "Anonymous",
                },
            ],
        ),
    }
    calls: list[str] = []
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_contributors("https://github.com/owner/people", limit=10, include_anonymous=True)

    assert data["state"] == "ready"
    assert data["anonymous_included"] is True
    assert data["contributors"][0] == {
        "login": "CatPlasma",
        "name": "",
        "email": "",
        "contributions": 120,
        "type": "User",
        "url": "https://github.com/CatPlasma",
        "avatar": "https://avatars.githubusercontent.com/u/1?v=4",
    }
    assert data["contributors"][1] == {
        "login": "",
        "name": "匿名提交者",
        "email": "anon@example.com",
        "contributions": 3,
        "type": "Anonymous",
        "url": "",
        "avatar": "",
    }
    assert "per_page=10" in calls[-1]
    assert "anon=1" in calls[-1]


async def test_read_contributors_reuses_recent_result_for_card(monkeypatch):
    calls: list[str] = []
    _patch_transport(monkeypatch, _handler({
        "/repos/owner/people": httpx.Response(200, json=_metadata()),
        "/repos/owner/people/contributors": httpx.Response(
            200, json=[{"login": "author", "contributions": 8}],
        ),
    }, calls))
    result = await read_contributors("https://github.com/owner/people", limit=5)
    calls.clear()

    assert await read_contributors(
        "https://github.com/Owner/People", reuse_recent=True
    ) == result
    assert calls == []


@pytest.mark.parametrize("headers, use_page", [
    ({"x-ratelimit-remaining": "0"}, True),
    ({"retry-after": "60", "x-ratelimit-remaining": "0"}, False),
    ({}, False),
])
async def test_read_contributors_web_fallback(monkeypatch, headers, use_page):
    calls: list[str] = []
    html = """
        <div class="BorderGrid-cell">
          <a href="/unrelated" data-hovercard-url="/users/unrelated/hovercard">Other</a>
        </div>
        <div class="BorderGrid-cell">
          <h2><a href="https://github.com/owner/people/graphs/contributors">Contributors</a></h2>
          <a href="/sample-user" data-hovercard-url="/users/sample-user/hovercard">User</a>
          <a href="https://github.com/sample-user" data-hovercard-url="/users/sample-user/hovercard">Duplicate</a>
        </div>
    """
    _patch_transport(monkeypatch, _handler({
        "/repos/owner/people": httpx.Response(403, headers=headers),
        "/owner/people": httpx.Response(200, text=html, headers={"content-type": "text/html"}),
    }, calls))
    if not use_page:
        with pytest.raises(GitHubError):
            await read_contributors("https://github.com/owner/people")
        assert calls == ["https://api.github.com/repos/owner/people"]
        return

    result = await read_contributors("https://github.com/owner/people")
    assert result["source"] == "github_web"
    assert result["contributors"] == [{
        "login": "sample-user", "contributions": None, "url": "https://github.com/sample-user",
    }]
    assert calls[-1] == "https://github.com/owner/people"


async def test_read_contributors_reports_pending_statistics(monkeypatch):
    routes = {
        "/repos/owner/fresh": httpx.Response(200, json=_metadata(full_name="owner/fresh")),
        "/repos/owner/fresh/contributors": httpx.Response(202),
    }
    _patch_transport(monkeypatch, _handler(routes, []))

    data = await read_contributors("https://github.com/owner/fresh")

    assert data["state"] == "pending"
    assert data["contributors"] == []
    assert "read_commits" in data["note"]


async def test_read_issues_filters_pull_requests_and_filters_labels(monkeypatch):
    routes = {
        "/repos/owner/board": httpx.Response(200, json=_metadata(full_name="owner/board")),
        "/repos/owner/board/issues": httpx.Response(
            200,
            json=[
                {
                    "number": 12,
                    "title": "繁殖逻辑异常",
                    "state": "open",
                    "state_reason": None,
                    "user": {"login": "ChiYuKe"},
                    "labels": [{"name": "bug"}, {"name": "critical"}],
                    "comments": 4,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-02T00:00:00Z",
                    "html_url": "https://github.com/owner/board/issues/12",
                },
                {
                    "number": 13,
                    "title": "修复 PR",
                    "state": "open",
                    "pull_request": {"url": "https://api.github.com/repos/owner/board/pulls/13"},
                    "html_url": "https://github.com/owner/board/pull/13",
                },
                {
                    "number": 14,
                    "title": "另一个 issue",
                    "state": "open",
                    "user": {"login": "BlazeCat"},
                    "labels": [],
                    "comments": 0,
                    "html_url": "https://github.com/owner/board/issues/14",
                },
            ],
        ),
    }
    calls: list[str] = []
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_issues("https://github.com/owner/board", state="all", labels="bug,critical", limit=1)

    assert data["scanned"] == 3
    assert data["pull_requests_skipped"] == 1
    assert [issue["number"] for issue in data["issues"]] == [12]
    assert data["issues"][0]["labels"] == ["bug", "critical"]
    assert data["issues"][0]["author"] == "ChiYuKe"
    assert data["next_page"] is None
    assert "per_page=20" in calls[-1]
    assert "labels=bug%2Ccritical" in calls[-1]


async def test_read_issues_validates_state_and_labels():
    with pytest.raises(GitHubError, match="state"):
        await read_issues("https://github.com/owner/repo", state="archived")
    with pytest.raises(GitHubError, match="sort"):
        await read_issues("https://github.com/owner/repo", sort="stars")
    with pytest.raises(GitHubError, match="labels"):
        await read_issues(
            "https://github.com/owner/repo", labels=",".join(f"label{index}" for index in range(6))
        )


async def test_read_pull_requests_reports_merge_state(monkeypatch):
    routes = {
        "/repos/owner/merge": httpx.Response(200, json=_metadata(full_name="owner/merge")),
        "/repos/owner/merge/pulls": httpx.Response(
            200,
            json=[
                {
                    "number": 2,
                    "title": "合并修复",
                    "state": "closed",
                    "draft": False,
                    "merged_at": "2026-02-02T00:00:00Z",
                    "user": {"login": "BlazeCat"},
                    "head": {"label": "BlazeCat:fix"},
                    "base": {"label": "owner:main"},
                    "created_at": "2026-02-01T00:00:00Z",
                    "updated_at": "2026-02-02T00:00:00Z",
                    "html_url": "https://github.com/owner/merge/pull/2",
                },
                {
                    "number": 3,
                    "title": "草稿",
                    "state": "open",
                    "draft": True,
                    "merged_at": None,
                    "user": {"login": "CatPlasma"},
                    "head": {"label": "CatPlasma:feat"},
                    "base": {"label": "owner:main"},
                    "created_at": "2026-03-01T00:00:00Z",
                    "updated_at": "2026-03-02T00:00:00Z",
                    "html_url": "https://github.com/owner/merge/pull/3",
                },
            ],
        ),
    }
    _patch_transport(monkeypatch, _handler(routes, []))

    data = await read_pull_requests("https://github.com/owner/merge", state="all", limit=2)

    assert data["pull_requests"][0]["merged"] is True
    assert data["pull_requests"][0]["head"] == "BlazeCat:fix"
    assert data["pull_requests"][1]["draft"] is True
    assert data["pull_requests"][1]["merged"] is False
    # 返回条数等于 limit 时可能还有下一页，不能断言已经取完。
    assert data["next_page"] == 2


async def test_read_issue_reads_body_and_latest_comments(monkeypatch):
    routes = {
        "/repos/owner/talk": httpx.Response(200, json=_metadata(full_name="owner/talk")),
        "/repos/owner/talk/issues/7": httpx.Response(
            200,
            json={
                "number": 7,
                "title": "启动崩溃",
                "state": "closed",
                "state_reason": "completed",
                "user": {"login": "ChiYuKe"},
                "labels": [{"name": "bug"}],
                "comments": 6,
                "created_at": "2026-03-01T00:00:00Z",
                "updated_at": "2026-03-03T00:00:00Z",
                "closed_at": "2026-03-03T00:00:00Z",
                "body": "复现步骤：\n1. 启动\n2. 崩溃",
                "html_url": "https://github.com/owner/talk/issues/7",
            },
        ),
        "/repos/owner/talk/issues/7/comments": httpx.Response(
            200,
            json=[
                {
                    "user": {"login": "CatPlasma"},
                    "created_at": "2026-03-02T00:00:00Z",
                    "body": "已修复",
                    "html_url": "https://github.com/owner/talk/issues/7#issuecomment-1",
                },
                {
                    "user": {"login": "BlazeCat"},
                    "created_at": "2026-03-03T00:00:00Z",
                    "body": "确认可用",
                    "html_url": "https://github.com/owner/talk/issues/7#issuecomment-2",
                },
            ],
        ),
    }
    calls: list[str] = []
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_issue("https://github.com/owner/talk", number=7, comments=2)

    assert data["is_pull_request"] is False
    assert data["state"] == "closed"
    assert data["state_reason"] == "completed"
    assert data["body"].startswith("复现步骤")
    assert data["labels"] == ["bug"]
    assert [comment["author"] for comment in data["recent_comments"]] == ["CatPlasma", "BlazeCat"]
    assert data["comments_truncated"] is True
    assert "per_page=2" in calls[-1]
    assert "page=3" in calls[-1]


async def test_read_issue_includes_pull_request_stats(monkeypatch):
    routes = {
        "/repos/owner/pr": httpx.Response(200, json=_metadata(full_name="owner/pr")),
        "/repos/owner/pr/issues/8": httpx.Response(
            200,
            json={
                "number": 8,
                "title": "新增工具",
                "state": "closed",
                "state_reason": "completed",
                "user": {"login": "CatPlasma"},
                "labels": [],
                "comments": 0,
                "pull_request": {"merged_at": "2026-03-01T00:00:00Z"},
                "body": "改动说明",
                "html_url": "https://github.com/owner/pr/pull/8",
            },
        ),
        "/repos/owner/pr/pulls/8": httpx.Response(
            200,
            json={
                "merged": True,
                "merged_at": "2026-03-01T00:00:00Z",
                "draft": False,
                "mergeable_state": "clean",
                "additions": 120,
                "deletions": 8,
                "changed_files": 3,
                "commits": 4,
                "head": {"label": "CatPlasma:feat"},
                "base": {"label": "owner:main"},
            },
        ),
    }
    _patch_transport(monkeypatch, _handler(routes, []))

    data = await read_issue("https://github.com/owner/pr", number=8)

    assert data["is_pull_request"] is True
    assert data["pull_request"]["merged"] is True
    assert data["pull_request"]["merged_at"] == "2026-03-01T00:00:00Z"
    assert data["pull_request"]["additions"] == 120
    assert data["pull_request"]["base"] == "owner:main"
    assert data["recent_comments"] == []


async def test_read_commit_resolves_branch_and_returns_patch(monkeypatch):
    routes = {
        "/repos/owner/diff": httpx.Response(200, json=_metadata(full_name="owner/diff")),
        "/repos/owner/diff/commits": httpx.Response(200, json=[{"sha": "abcdef1234567890"}]),
        "/repos/owner/diff/commits/abcdef1234567890": httpx.Response(
            200,
            json={
                "sha": "abcdef1234567890",
                "html_url": "https://github.com/owner/diff/commit/abcdef1",
                "author": {"login": "CatPlasma"},
                "commit": {
                    "message": "修复繁殖逻辑\n\n补充边界判断",
                    "author": {"name": "CatPlasma", "date": "2026-04-01T10:00:00Z"},
                    "committer": {"name": "GitHub", "date": "2026-04-01T10:00:00Z"},
                },
                "parents": [{"sha": "1111111222222222"}],
                "stats": {"additions": 12, "deletions": 3, "total": 15},
                "files": [
                    {
                        "filename": "src/Mod.cs",
                        "status": "modified",
                        "additions": 10,
                        "deletions": 3,
                        "patch": "@@ -1 +1 @@\n-旧\n+新",
                    },
                    {
                        "filename": "README.md",
                        "status": "modified",
                        "additions": 2,
                        "deletions": 0,
                        "patch": "@@ -1 +1 @@\n-文档\n+文档2",
                    },
                ],
            },
        ),
    }
    calls: list[str] = []
    _patch_transport(monkeypatch, _handler(routes, calls))

    data = await read_commit("https://github.com/owner/diff", ref="feature/fix")

    assert any("sha=feature%2Ffix" in url for url in calls)
    assert data["sha"] == "abcdef1234567890"
    assert data["short_sha"] == "abcdef1"
    assert data["message"] == "修复繁殖逻辑"
    assert data["body"] == "补充边界判断"
    assert data["author"] == {
        "name": "CatPlasma",
        "login": "CatPlasma",
        "avatar": "",
        "date": "2026-04-01T10:00:00Z",
    }
    assert data["stats"] == {"additions": 12, "deletions": 3, "total": 15}
    assert data["parents"] == ["1111111"]
    assert data["files"][0]["filename"] == "src/Mod.cs"
    assert data["files"][0]["patch"].startswith("@@")
    assert data["files_total"] == 2
    assert data["patch_truncated"] is False


async def test_read_issue_stays_within_output_budget(monkeypatch):
    routes = {
        "/repos/owner/thready": httpx.Response(200, json=_metadata(full_name="owner/thready")),
        "/repos/owner/thready/issues/9": httpx.Response(
            200,
            json={
                "number": 9,
                "title": "长讨论",
                "state": "closed",
                "user": {"login": "ChiYuKe"},
                "labels": [{"name": "bug"}],
                "comments": 25,
                "body": "b" * 3000,
                "html_url": "https://github.com/owner/thready/issues/9",
            },
        ),
        "/repos/owner/thready/issues/9/comments": httpx.Response(
            200,
            json=[
                {
                    "user": {"login": f"user{index}"},
                    "created_at": "2026-03-01T00:00:00Z",
                    "body": "c" * 700,
                    "html_url": "",
                }
                for index in range(20)
            ],
        ),
    }
    _patch_transport(monkeypatch, _handler(routes, []))

    data = await read_issue("https://github.com/owner/thready", number=9, comments=20)

    assert len(json.dumps(data, ensure_ascii=False)) <= 18000
    assert data["comments_truncated"] is True
    assert data["recent_comments"]


async def test_read_commit_truncates_long_patch(monkeypatch):
    routes = {
        "/repos/owner/bigdiff": httpx.Response(200, json=_metadata(full_name="owner/bigdiff")),
        "/repos/owner/bigdiff/commits/abcdef123456": httpx.Response(
            200,
            json={
                "sha": "abcdef123456",
                "commit": {"message": "大改", "author": {}, "committer": {}},
                "files": [
                    {
                        "filename": "a.cs",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "patch": "p" * 5000,
                    }
                ],
            },
        ),
    }
    _patch_transport(monkeypatch, _handler(routes, []))

    data = await read_commit("https://github.com/owner/bigdiff", ref="abcdef123456")

    assert len(data["files"][0]["patch"]) == 1200
    assert data["files"][0]["patch_truncated"] is True
    assert data["patch_truncated"] is True
    assert data["stats"] == {"additions": 0, "deletions": 0, "total": 0}


async def test_read_branches_lists_protected_and_default(monkeypatch):
    routes = {
        "/repos/owner/branches": httpx.Response(200, json=_metadata(full_name="owner/branches")),
        "/repos/owner/branches/branches": httpx.Response(
            200,
            json=[
                {"name": "main", "protected": True, "commit": {"sha": "1111111222222222"}},
                {"name": "dev", "protected": False, "commit": {"sha": "3333333444444444"}},
            ],
        ),
    }
    _patch_transport(monkeypatch, _handler(routes, []))

    data = await read_branches("https://github.com/owner/branches", limit=5)

    assert data["default_branch"] == "main"
    assert data["branches"] == [
        {"name": "main", "protected": True, "commit": "1111111"},
        {"name": "dev", "protected": False, "commit": "3333333"},
    ]


async def test_issue_number_validation_happens_before_network():
    with pytest.raises(GitHubError, match="number"):
        await read_issue("https://github.com/owner/repo", number=0)
    with pytest.raises(GitHubError, match="sort"):
        await read_pull_requests("https://github.com/owner/repo", sort="comments")


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
        "search_code",
        "read_releases",
        "read_commits",
        "read_contributors",
        "read_issues",
        "read_pull_requests",
        "read_issue",
        "read_commit",
        "read_branches",
        "render_card",
    ]
    assert {setting.key for setting in plugin.settings.fields.values()} >= {
        "token",
        "timeout",
        "cache_seconds",
        "font_path",
    }
    assert plugin.agent_instructions[0].name == "repository_analysis"
    assert plugin.agent_instructions[0].tools == ("read_repository",)
    assert "read_contributors" in plugin.agent_instructions[0].text
    assert "render_card" in plugin.agent_instructions[0].text
    read_only = [tool for tool in plugin.ai_tools if tool.name != "render_card"]
    assert all(tool.effect == "read_only" for tool in read_only)
    render_tool = next(tool for tool in plugin.ai_tools if tool.name == "render_card")
    assert render_tool.effect == "side_effecting"
    assert set(render_tool.capabilities) == {"net.read", "qq.send"}
    assert set(render_tool.parameters["properties"]["kind"]["enum"]) == set(
        ("repository", "contributors", "issues", "pull_requests", "issue", "commit", "releases", "branches")
    )
    properties = plugin.ai_tools[0].parameters["properties"]
    assert {"repository", "view", "depth", "tree_path"} <= set(properties)


async def test_render_card_tool_renders_and_sends(monkeypatch):
    import importlib
    from types import SimpleNamespace

    module = importlib.import_module("fllbot.builtin_plugins.github.plugin")
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "send_media_reply", sender)
    tiny_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    routes = {
        "/repos/owner/people": httpx.Response(200, json=_metadata(full_name="owner/people")),
        "/repos/owner/people/contributors": httpx.Response(
            200,
            json=[
                {
                    "login": "CatPlasma",
                    "contributions": 120,
                    "type": "User",
                    "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4",
                },
                {"login": "BlazeCat", "contributions": 30, "type": "User"},
            ],
        ),
        "/u/1": httpx.Response(200, content=tiny_png, headers={"content-type": "image/png"}),
    }
    _patch_transport(monkeypatch, _handler(routes, []))
    ctx = SimpleNamespace(settings=SimpleNamespace(get=lambda key, default=None: None))
    tool = next(item for item in module.plugin.ai_tools if item.name == "render_card")

    result = await tool.callback(
        ctx, "https://github.com/owner/people", kind="contributors", limit=5
    )

    assert result["sent"] is True
    assert result["kind"] == "contributors"
    assert result["bytes"] > 1000
    args, kwargs = sender.call_args
    assert args[0] is ctx
    assert "贡献者榜单" in args[2]
    assert kwargs == {"require_ai": True}
    image = base64.b64decode(args[1][0].data["file"].removeprefix("base64://"))
    assert image.startswith(b"\x89PNG")

    sender.reset_mock()
    failed = await tool.callback(ctx, "https://github.com/owner/people", kind="issue")
    assert "number" in failed["error"]
    sender.assert_not_called()

    sender.return_value = False
    result = await tool.callback(ctx, "https://github.com/owner/people", kind="contributors")
    assert result["sent"] is False


async def test_plugin_tool_callback_wraps_errors(monkeypatch):
    from types import SimpleNamespace

    from fllbot.builtin_plugins.github import plugin

    calls: list[str] = []
    _patch_transport(monkeypatch, _handler({}, calls))
    ctx = SimpleNamespace(settings=SimpleNamespace(get=lambda key, default=None: None))

    result = await plugin.ai_tools[0].callback(ctx, "https://github.com/owner")

    assert "error" in result
    assert result["repository"] == "https://github.com/owner"
