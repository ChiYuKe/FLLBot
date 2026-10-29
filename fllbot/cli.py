"""命令行入口：``fllbot run`` / ``init`` / ``check``。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .app import FLLBot, create_app
from .config import ConfigStore, load_config_dict, mask_secrets, resolve_path
from .logging_setup import configure_logging

_MINIMAL_CONFIG = """# FLLBot 配置（由 fllbot init 生成）
[server]
host = "127.0.0.1"
port = 6701
ws_path = "/onebot/v11/ws"
access_token = ""

[bot]
superusers = []
command_prefix = ["/"]

[plugin]
dirs = ["plugins"]
builtin = true
hot_reload = true

[storage]
path = "data/fllbot.db"

[logging]
level = "INFO"
dir = "logs"

[llm]
enabled = false
base_url = "https://api.deepseek.com"
api_key = ""
model = "deepseek-chat"
"""

_EXAMPLE_PLUGIN = '''"""示例插件：改动保存后会自动热重载。"""

from fllbot.plugin import Context, Plugin

plugin = Plugin(name="hello", version="0.1.0", description="示例插件：/hello 打个招呼")


@plugin.on_command("hello", aliases=("你好",))
async def hello(ctx: Context) -> None:
    await ctx.reply(f"你好，{ctx.nickname}！我是 FLLBot。", quote=True)
'''


def _configure_stdio() -> None:
    """Windows 控制台编码可能不支持特殊字符，避免打印时异常退出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # pragma: no cover
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fllbot", description="FLLBot —— 面向 NapCat 的 QQ 机器人后端")
    parser.add_argument("-V", "--version", action="version", version=f"fllbot {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="启动后端（默认命令）")
    run.add_argument("--config", default="config.toml", help="配置文件路径")
    run.add_argument("--root", default=".", help="项目根目录")
    run.add_argument("--host", default=None, help="覆盖监听地址")
    run.add_argument("--port", type=int, default=None, help="覆盖监听端口")

    init = sub.add_parser("init", help="初始化目录结构与示例配置")
    init.add_argument("--root", default=".")
    init.add_argument("--force", action="store_true", help="覆盖已有配置文件")

    check = sub.add_parser("check", help="校验配置并打印摘要")
    check.add_argument("--config", default="config.toml")
    check.add_argument("--root", default=".")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    if command == "run":
        return cmd_run(args)
    if command == "init":
        return cmd_init(args)
    if command == "check":
        return cmd_check(args)
    parser.print_help()
    return 1


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    base = load_config_dict(args.config, root=root)
    if args.host:
        base.setdefault("server", {})["host"] = args.host
    if args.port:
        base.setdefault("server", {})["port"] = int(args.port)

    store = ConfigStore(base)
    config = store.current
    configure_logging(
        level=config.logging.level,
        log_dir=resolve_path(root, config.logging.dir),
        json_mode=config.logging.json_lines,
        filename=config.logging.file,
        ring_size=config.logging.ring_size,
    )
    logger = logging.getLogger("fllbot")
    logger.info("启动 FLLBot %s（根目录 %s）", __version__, root)

    bot = FLLBot(store, root)
    app = create_app(bot)

    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print("缺少 uvicorn，请先执行：pip install -e .", file=sys.stderr)
        return 1

    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    return 0


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ("plugins", "personas", "data", "logs"):
        (root / name).mkdir(exist_ok=True)

    config_path = root / "config.toml"
    if config_path.exists() and not args.force:
        print(f"配置已存在，跳过：{config_path}")
    else:
        example = _find_example()
        content = example.read_text(encoding="utf-8") if example else _MINIMAL_CONFIG
        config_path.write_text(content, encoding="utf-8")
        print(f"已写入配置：{config_path}")

    plugin_path = root / "plugins" / "hello.py"
    if not plugin_path.exists():
        plugin_path.write_text(_EXAMPLE_PLUGIN, encoding="utf-8")
        print(f"已写入示例插件：{plugin_path}")

    env_path = root / ".env.example"
    if not env_path.exists():
        env_path.write_text(
            "# 复制为 .env 后填写；也可用系统环境变量\n"
            "FLLBOT_LLM__API_KEY=\n"
            "FLLBOT_SERVER__PORT=6701\n",
            encoding="utf-8",
        )
        print(f"已写入环境变量示例：{env_path}")

    print(
        "\n下一步：\n"
        "  1. 编辑 config.toml，填写 bot.superusers 与 llm 相关项\n"
        "  2. 启动：fllbot run --root .\n"
        "  3. NapCat 里添加「反向 WebSocket」：ws://127.0.0.1:6701/onebot/v11/ws\n"
        "  4. 管理 API：http://127.0.0.1:6701/api/docs（令牌见启动日志或 data/admin_token.txt）"
    )
    return 0


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #


def cmd_check(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    base = load_config_dict(args.config, root=root)
    try:
        store = ConfigStore(base)
    except Exception as exc:
        print(f"配置校验失败：{exc}", file=sys.stderr)
        return 1
    config = store.current

    summary: dict[str, Any] = {
        "version": __version__,
        "root": str(root),
        "config_file": str(resolve_path(root, args.config)),
        "config_exists": resolve_path(root, args.config).is_file(),
        "ws": f"ws://{config.server.host}:{config.server.port}{config.server.ws_path}",
        "auth_required": bool(config.server.access_token),
        "plugins_dir": [str(resolve_path(root, item)) for item in config.plugin.dirs],
        "storage": str(resolve_path(root, config.storage.path)),
        "builtin_plugins": config.plugin.builtin,
        "hot_reload": config.plugin.hot_reload,
        "superusers": config.bot.superusers,
        "command_prefix": config.bot.command_prefix,
        "llm": {
            "enabled": config.llm.enabled,
            "model": config.llm.model,
            "base_url": config.llm.base_url,
            "api_key": "已配置" if config.llm.api_key else "缺失",
            "trigger": config.llm.trigger,
        },
        "admin_token": "已配置" if (config.admin.token or resolve_path(root, config.admin.token_file).is_file()) else "首次启动时生成",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    problems: list[str] = []
    if config.llm.enabled and not config.llm.api_key:
        problems.append("llm.enabled = true 但没有配置 api_key")
    if config.server.access_token and len(config.server.access_token) < 8:
        problems.append("server.access_token 过短（建议至少 8 位）")
    for item in config.plugin.dirs:
        directory = resolve_path(root, item)
        if not directory.is_dir():
            problems.append(f"插件目录不存在（启动时会自动创建）：{directory}")
    if problems:
        print("\n需要注意：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
    else:
        print("\n配置检查通过。")
    print("\n生效配置（密钥已隐藏）：")
    print(json.dumps(mask_secrets(config), ensure_ascii=False, indent=2))
    return 0


def _find_example() -> Path | None:
    candidates = [
        Path(__file__).resolve().parent.parent / "config.example.toml",
        Path.cwd() / "config.example.toml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
