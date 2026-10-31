"""离线可用、可嵌入桌面端的管理控制台；按职责分文件维护，交付时内联。"""

from pathlib import Path

from . import __version__

_UI = Path(__file__).with_name("ui")


def render_index() -> str:
    """组装页面；管理数据由带令牌的 API 请求提供。"""
    html = (_UI / "index.html").read_text(encoding="utf-8")
    styles = "\n".join((_UI / name).read_text(encoding="utf-8") for name in (
        "console.css", "controls.css", "desktop.css", "scrollbars.css",
    ))
    script = (_UI / "app.js").read_text(encoding="utf-8")
    features = "\n".join((_UI / name).read_text(encoding="utf-8") for name in (
        "console.js", "navigation.js", "policy.js", "chat-settings.js", "group-table.js",
    ))
    script = script.replace("/* CONSOLE_FEATURES */", features)
    script += "\n" + (_UI / "desktop-theme.js").read_text(encoding="utf-8")
    return html.replace("__VERSION__", __version__).replace("__STYLES__", styles).replace("__SCRIPT__", script)


__all__ = ["render_index"]
