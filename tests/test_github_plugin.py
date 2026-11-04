"""GitHub repository tool: URL validation, bounded API reads and actionable errors."""

import base64

import httpx
import pytest

from fllbot.builtin_plugins.github.service import GitHubError, parse_repository, read_repository


def test_repository_url_validation():
    assert parse_repository("https://github.com/ChiYuKe/ONI-Mods") == ("ChiYuKe", "ONI-Mods")
    assert parse_repository("https://github.com/ChiYuKe/ONI-Mods/tree/master/README.md") == ("ChiYuKe", "ONI-Mods")
    assert parse_repository("git@github.com:ChiYuKe/ONI-Mods.git") == ("ChiYuKe", "ONI-Mods")
    for url in (
        "https://github.com.evil.invalid/owner/repo",
        "http://github.com/owner/repo",
        "https://u:p@github.com/owner/repo",
        "https://github.com/owner",
    ):
        with pytest.raises(GitHubError):
            parse_repository(url)


async def test_read_repository_returns_readme_tree_and_images(monkeypatch):
    from fllbot.builtin_plugins.github import service

    seen = []
    readme = "# 示例仓库\n\n<img src=x>"

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(
                200,
                json={
                    "private": False,
                    "default_branch": "main",
                    "html_url": "https://github.com/owner/repo",
                    "description": "demo",
                    "stargazers_count": 2,
                },
            )
        if request.url.path.endswith("/readme"):
            return httpx.Response(
                200, json={"encoding": "base64", "content": base64.b64encode(readme.encode()).decode()}
            )
        if request.url.path.endswith("/git/trees/main"):
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "README.md", "type": "blob"},
                        {"path": "assets/demo.png", "type": "blob"},
                        {"path": "docs/guide.md", "type": "blob"},
                    ]
                },
            )
        if request.url.path.endswith("/contents/docs/guide.md"):
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"# Guide").decode()})
        return httpx.Response(404, json={"message": "Not Found"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        service.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    data = await read_repository("https://github.com/owner/repo", paths=["docs/guide.md"])
    assert data["readme"] == readme
    assert data["images"] == ["assets/demo.png"]
    assert data["selected_files"] == {"docs/guide.md": "# Guide"}
    assert data["description"] == "demo"
    assert len(seen) == 4


async def test_github_errors_are_specific():
    # 内置插件可能被加载器重新执行，模块对象会被替换；同时取错误类型与函数，
    # 避免和其他用例持有的旧模块引用混用。
    from fllbot.builtin_plugins.github import service

    class Client:
        def __init__(self, response):
            self.response = response

        async def get(self, *args, **kwargs):
            return self.response

    for response, expected in (
        (httpx.Response(404), "不存在"),
        (httpx.Response(403, headers={"x-ratelimit-remaining": "0"}), "速率上限"),
        (httpx.Response(403), "拒绝"),
    ):
        response.request = httpx.Request("GET", "https://api.github.com/repos/owner/repo")
        with pytest.raises(service.GitHubError, match=expected):
            await service._get(Client(response), "https://api.github.com/repos/owner/repo", "")


def test_builtin_manifest_and_agent_tool():
    from fllbot.builtin_plugins.github import plugin

    assert not plugin.handlers
    assert [tool.name for tool in plugin.ai_tools] == ["read_repository", "read_sources"]
