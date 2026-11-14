"""GitHub 图片卡片：各类型 PNG 输出、折行、边界校验与空数据。"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from fllbot.builtin_plugins.github.render import KINDS, _wrap, render_card
from fllbot.rendering.text_image import load_font

REPOSITORY = {
    "repository": "https://github.com/ChiYuKe/ONI-Mods",
    "full_name": "ChiYuKe/ONI-Mods",
    "description": "缺氧模组集合，提供中文说明与安装脚本。",
    "default_branch": "master",
    "stars": 1287,
    "forks": 96,
    "open_issues": 12,
    "language": "C#",
    "topics": ["oni", "mods"],
    "license": "MIT",
    "archived": False,
    "pushed_at": "2026-11-12T08:30:00Z",
    "created_at": "2024-03-02T00:00:00Z",
    "homepage": "",
    "directories": [{"path": "src", "files": 120}, {"path": "docs", "files": 34}],
}

CONTRIBUTORS = {
    "repository": "https://github.com/ChiYuKe/ONI-Mods",
    "contributors": [
        {"login": "CatPlasma", "contributions": 1204, "type": "User"},
        {"login": "BlazeCat", "contributions": 486, "type": "User"},
        {"login": "ChiYuKe", "contributions": 210, "type": "User"},
    ],
    "anonymous_included": False,
}

ISSUES = {
    "repository": "https://github.com/ChiYuKe/ONI-Mods",
    "state": "open",
    "pull_requests_skipped": 4,
    "issues": [
        {
            "number": 128,
            "title": "启动时崩溃：NullReferenceException in ModLoader.Init",
            "state": "open",
            "author": "ChiYuKe",
            "labels": ["bug", "crash"],
            "comments": 7,
            "updated_at": "2026-11-11T12:00:00Z",
        },
        {
            "number": 120,
            "title": "README 的安装步骤在 Linux 下不适用",
            "state": "closed",
            "author": "BlazeCat",
            "labels": ["docs"],
            "comments": 4,
            "updated_at": "2026-10-30T09:00:00Z",
        },
    ],
}

PULLS = {
    "repository": "https://github.com/ChiYuKe/ONI-Mods",
    "state": "all",
    "pull_requests": [
        {
            "number": 131,
            "title": "修复 Spaced Out 建筑注册失败",
            "state": "open",
            "draft": False,
            "merged": False,
            "author": "CatPlasma",
            "head": "CatPlasma:fix/spaced-out",
            "base": "ChiYuKe:master",
            "updated_at": "2026-11-12T10:00:00Z",
        },
        {
            "number": 129,
            "title": "更新中文翻译",
            "state": "closed",
            "draft": False,
            "merged": True,
            "author": "ChiYuKe",
            "head": "ChiYuKe:i18n",
            "base": "ChiYuKe:master",
            "updated_at": "2026-11-08T10:00:00Z",
        },
    ],
}

ISSUE = {
    "repository": "https://github.com/ChiYuKe/ONI-Mods",
    "number": 128,
    "title": "启动时崩溃：NullReferenceException in ModLoader.Init",
    "state": "closed",
    "state_reason": "completed",
    "author": "ChiYuKe",
    "labels": ["bug", "crash"],
    "is_pull_request": False,
    "created_at": "2026-11-01T02:00:00Z",
    "updated_at": "2026-11-11T12:00:00Z",
    "closed_at": "2026-11-11T12:00:00Z",
    "comments": 7,
    "body": "复现步骤：\n1. 安装最新版 ModLoader\n2. 启动游戏并加载存档\n3. 进入第二周期后崩溃",
    "recent_comments": [
        {"author": "CatPlasma", "created_at": "2026-11-10T08:00:00Z", "body": "已定位到注册顺序问题。"},
        {"author": "BlazeCat", "created_at": "2026-11-11T09:00:00Z", "body": "修复已合并，麻烦更新后确认。"},
    ],
}

COMMIT = {
    "repository": "https://github.com/ChiYuKe/ONI-Mods",
    "ref": "master",
    "sha": "beef1234567890abcdef",
    "short_sha": "beef123",
    "message": "修复 DLC 建筑补丁注册顺序",
    "body": "把建筑补丁注册移到 DLC 初始化之后，并补充回归测试。",
    "author": {"name": "CatPlasma", "login": "CatPlasma", "date": "2026-11-12T08:30:00Z"},
    "stats": {"additions": 128, "deletions": 37, "total": 165},
    "files_total": 3,
    "patch_truncated": False,
    "files": [
        {"filename": "src/ModLoader.cs", "status": "modified", "additions": 42, "deletions": 18},
        {"filename": "src/Patches/BuildingPatch.cs", "status": "added", "additions": 61, "deletions": 0},
        {"filename": "docs/changelog.md", "status": "renamed", "additions": 0, "deletions": 0},
    ],
}

RELEASES = {
    "repository": "https://github.com/ChiYuKe/ONI-Mods",
    "releases": [
        {
            "tag": "v2.4.0",
            "name": "春季更新",
            "published_at": "2026-03-01T00:00:00Z",
            "body": "新增 Spaced Out 建筑支持，修复启动崩溃。",
            "assets": [{"name": "ONI-Mods-v2.4.0.zip", "size": 2048000}],
        }
    ],
}

BRANCHES = {
    "repository": "https://github.com/ChiYuKe/ONI-Mods",
    "default_branch": "master",
    "branches": [
        {"name": "master", "protected": True, "commit": "beef123"},
        {"name": "feature/spaced-out", "protected": False, "commit": "dead789"},
    ],
}

CASES = {
    "repository": REPOSITORY,
    "contributors": CONTRIBUTORS,
    "issues": ISSUES,
    "pull_requests": PULLS,
    "issue": ISSUE,
    "commit": COMMIT,
    "releases": RELEASES,
    "branches": BRANCHES,
}


@pytest.mark.parametrize("kind", KINDS)
def test_render_card_outputs_png(kind):
    data = render_card(kind, CASES[kind])

    assert data.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.width == 960
        assert 220 <= image.height <= 4000


def test_render_card_draws_avatar_images():
    buffer = io.BytesIO()
    image = Image.new("RGB", (96, 96), "#0963da")
    ImageDraw.Draw(image).rectangle((0, 0, 48, 96), fill="#ffffff")
    image.save(buffer, format="PNG")
    url = "https://avatars.githubusercontent.com/u/1?v=4"
    payload = {
        **CONTRIBUTORS,
        "contributors": [
            {**item, "avatar": url} for item in CONTRIBUTORS["contributors"]
        ],
    }

    with_avatar = render_card("contributors", payload, avatars={url: buffer.getvalue()})
    without_avatar = render_card("contributors", payload)

    assert with_avatar.startswith(b"\x89PNG")
    assert with_avatar != without_avatar


def test_render_card_width_is_configurable():
    with Image.open(io.BytesIO(render_card("repository", REPOSITORY, width=720))) as image:
        assert image.width == 720


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("contributors", {"repository": "https://github.com/a/b", "contributors": []}),
        (
            "contributors",
            {"repository": "https://github.com/a/b", "contributors": [], "state": "pending"},
        ),
        ("issues", {"repository": "https://github.com/a/b", "issues": []}),
        ("branches", {"repository": "https://github.com/a/b", "branches": []}),
        ("releases", {"repository": "https://github.com/a/b", "releases": []}),
    ],
)
def test_render_card_handles_empty_lists(kind, payload):
    assert render_card(kind, payload).startswith(b"\x89PNG")


def test_render_card_rejects_unknown_kind_width_and_empty_payload():
    with pytest.raises(ValueError, match="图片类型"):
        render_card("graph", CONTRIBUTORS)
    with pytest.raises(ValueError, match="宽度"):
        render_card("contributors", CONTRIBUTORS, width=100)
    with pytest.raises(ValueError, match="没有可渲染"):
        render_card("contributors", {})


def test_wrap_preserves_newlines_and_marks_truncation():
    font = load_font("", 18)
    assert _wrap("第一行\n第二行", font, 400, 5) == ["第一行", "第二行"]
    wrapped = _wrap("字" * 200, font, 200, 2)
    assert len(wrapped) == 2
    assert wrapped[-1].endswith("…")
