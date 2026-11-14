"""GitHub Primer Octicons, rasterized from the upstream 16 px artwork."""

from functools import lru_cache
from pathlib import Path

from PIL import Image

_ASSETS = Path(__file__).with_name("assets") / "octicons"
_ALIASES = {
    "archive": "file-zip",
    "arrow_right": "arrow-right",
    "branch": "git-branch",
    "commit": "git-commit",
    "cross": "x",
    "draft": "git-pull-request-draft",
    "folder": "file-directory",
    "fork": "repo-forked",
    "image": "file-media",
    "issue": "issue-opened",
    "issue_closed": "issue-closed",
    "license": "law",
    "merge": "git-merge",
    "minus": "dash",
    "pr": "git-pull-request",
    "pr_closed": "git-pull-request-closed",
}
_NAMES = frozenset({
    "calendar", "check", "clock", "code", "comment", "dot", "download", "file",
    "mark-github", "package", "people", "person", "plus", "repo", "shield", "star", "tag", "x",
    *_ALIASES.values(),
})


@lru_cache(maxsize=len(_NAMES))
def _mask(name: str) -> Image.Image:
    with Image.open(_ASSETS / f"{name}.png") as source:
        return source.convert("RGBA").getchannel("A")


def draw_icon(draw, name: str, box: tuple[float, float, float, float], color: str) -> bool:
    """Paint the upstream silhouette in the caller's color."""
    key = _ALIASES.get(name, name)
    if key not in _NAMES:
        return False
    x0, y0, x1, y1 = box
    size = max(1, round(min(x1 - x0, y1 - y0)))
    mask = _mask(key).resize((size, size), Image.Resampling.LANCZOS)
    draw.bitmap((round((x0 + x1 - size) / 2), round((y0 + y1 - size) / 2)), mask, fill=color)
    return True
