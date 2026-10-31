r"""用独立数据目录预览管理后台，不连接 QQ 或调用真实模型。

用法：.venv\Scripts\python.exe tools/preview_console.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fllbot.app import FLLBot, create_app  # noqa: E402
from fllbot.config import ConfigStore  # noqa: E402

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / ".console-preview"
    bot = FLLBot(ConfigStore({
        "admin": {"token": "console-preview-only"},
        "server": {"port": 6719},
        "plugin": {"hot_reload": False, "dirs": []},
        "llm": {"enabled": False},
    }), root=root)
    uvicorn.run(create_app(bot), host="127.0.0.1", port=6719)
