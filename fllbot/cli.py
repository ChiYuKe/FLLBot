"""命令行入口：``fllbot run`` / ``init`` / ``check``。"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .app import FLLBot, create_app
from .config import ConfigStore, load_config_dict, mask_secrets, resolve_path
from .logging_setup import configure_logging
from .plugin.scaffold import check_package, create_package

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

    plugin = sub.add_parser("plugin", help="新建、校验和列出独立插件包")
    actions = plugin.add_subparsers(dest="plugin_action", required=True)
    new = actions.add_parser("new", help="生成带包头、文档、配置与处理器的插件包")
    new.add_argument("name")
    new.add_argument("--template", choices=("command", "tool"), default="command")
    new.add_argument("--description", default="请填写插件用途")
    new.add_argument("--author", default="")
    for action in (
        new,
        actions.add_parser("check", help="离线校验包结构和 Python 语法"),
        actions.add_parser("list", help="离线列出插件包头与路径"),
    ):
        action.add_argument("--root", default=".")
        action.add_argument("--directory", default="plugins", help="相对于 root 的插件根目录")
        if action is not new:
            action.add_argument("--name", default=None, help="只检查指定包")
            action.add_argument("--builtin", action="store_true", help="同时检查内置插件包")
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
    if command == "plugin":
        return cmd_plugin(args)
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

    if not port_available(config.server.host, config.server.port):
        print(
            f"端口已被占用：{config.server.host}:{config.server.port}\n"
            "可能是 NapCat 的端口，或已有一个 FLLBot 在运行。\n"
            "换一个端口：fllbot run --port 6702",
            file=sys.stderr,
        )
        return 2

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

    plugin_path = root / "plugins" / "hello"
    if not plugin_path.exists() and not (root / "plugins" / "hello.py").exists():
        create_package(root / "plugins", "hello", description="示例插件：/hello 打个招呼")
        print(f"已写入示例插件：{plugin_path}")

    env_path = root / ".env.example"
    if not env_path.exists():
        env_path.write_text(
            "# 复制为 .env 后填写；也可用系统环境变量\nFLLBOT_LLM__API_KEY=\nFLLBOT_SERVER__PORT=6701\n",
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
        "admin_token": "已配置"
        if (config.admin.token or resolve_path(root, config.admin.token_file).is_file())
        else "首次启动时生成",
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


def cmd_plugin(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    directory = resolve_path(root, args.directory)
    try:
        if args.plugin_action == "new":
            print(
                create_package(
                    directory, args.name, description=args.description, author=args.author, template=args.template
                )
            )
            return 0
        from .plugin.manifest import validate_name

        if args.name:
            validate_name(args.name)
        roots = [directory]
        if args.builtin:
            roots.append(Path(__file__).parent / "builtin_plugins")
        failed = False
        for base in roots:
            if not base.is_dir():
                print(f"ERROR  插件目录不存在：{base}", file=sys.stderr)
                failed = True
                continue
            candidates = (
                [base / args.name]
                if args.name
                else [
                    path
                    for path in sorted(base.iterdir())
                    if not path.name.startswith(("_", ".")) and (path.is_dir() or path.suffix == ".py")
                ]
            )
            for package in candidates:
                try:
                    if package.is_file():
                        raise ValueError("旧单文件插件：请迁移为独立目录包")
                    manifest = check_package(package)
                    if args.plugin_action == "list":
                        print(f"{manifest.name}\t{manifest.version}\t{manifest.category}\t{package}")
                    else:
                        print(f"OK  {manifest.name}: {package}")
                except (ValueError, OSError, SyntaxError) as error:
                    failed = True
                    print(f"ERROR  {package}: {error}", file=sys.stderr)
        return int(failed)
    except (ValueError, OSError, SyntaxError) as error:
        print(f"插件操作失败：{error}", file=sys.stderr)
        return 1


def _find_example() -> Path | None:
    candidates = [
        Path(__file__).resolve().parent.parent / "config.example.toml",
        Path.cwd() / "config.example.toml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def port_available(host: str, port: int) -> bool:
    """尝试绑定端口，判断能否启动（被占用返回 False）。"""
    if not port:
        return False
    host = host or "127.0.0.1"
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((host, int(port)))
    except OSError:
        return False
    return True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
