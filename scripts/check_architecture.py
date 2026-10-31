"""Static dependency checks for FLLBot core boundaries and independent plugin packages."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = {
    "config": {"config"},
    "events": {"events"},
    "logging_setup": {"logging_setup"},
    "storage": {"storage"},
    "onebot": {"onebot"},
    "rules": {"rules"},
    "permission": {"permission"},
    "policy": {"policy"},
    "llm": {"llm", "config", "onebot", "policy"},
    "plugin": {"plugin", "rules", "onebot"},
}


class RuntimeImports(ast.NodeVisitor):
    """Ignore annotation-only dependencies while checking function-local imports too."""

    def __init__(self):
        self.items: list[ast.Import | ast.ImportFrom] = []

    def visit_If(self, node: ast.If) -> None:
        if (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING") or (
            isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
        ):
            for item in node.orelse:
                self.visit(item)
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.items.append(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.items.append(node)


def imports(source: Path, module: str):
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    visitor = RuntimeImports()
    visitor.visit(tree)
    parts = module.split(".") if source.name == "__init__.py" else module.split(".")[:-1]
    for node in visitor.items:
        if isinstance(node, ast.Import):
            for item in node.names:
                yield node.lineno, item.name
        else:
            prefix = parts[: len(parts) - node.level + 1] if node.level else []
            target = ".".join(prefix + ([node.module] if node.module else []))
            yield node.lineno, target
            for item in node.names:
                yield node.lineno, f"{target}.{item.name}"


def check(root: Path = ROOT) -> list[str]:
    errors = []
    core = root / "fllbot"
    for path in core.rglob("*.py"):
        relative = path.relative_to(core)
        layer = relative.parts[0] if len(relative.parts) > 1 else path.stem
        if layer not in BOUNDARIES:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        for line, target in imports(path, ".".join(["fllbot", *parts])):
            if target.startswith("fllbot.") and target.split(".")[1] not in BOUNDARIES[layer]:
                errors.append(f"{path.relative_to(root)}:{line}: {layer} 不能反向依赖 {target}")
    packages = root / "plugins"
    if packages.is_dir():
        for package in packages.iterdir():
            if not package.is_dir() or package.name.startswith((".", "_")):
                continue
            for path in package.rglob("*.py"):
                relative = path.relative_to(package).with_suffix("")
                parts = list(relative.parts)
                if parts[-1] == "__init__":
                    parts.pop()
                module = ".".join(["plugins", package.name, *parts])
                for line, target in imports(path, module):
                    cross_package = target.startswith("plugins.") and target.split(".")[1] != package.name
                    internal_host = target.startswith(
                        ("fllbot.app", "fllbot.api", "fllbot.builtin_plugins", "fllbot_user_plugins")
                    )
                    if cross_package or internal_host:
                        errors.append(
                            f"{path.relative_to(root)}:{line}: 插件包通过 SDK/Context 协作，不能导入 {target}"
                        )
    return list(dict.fromkeys(errors))


if __name__ == "__main__":
    failures = check()
    for failure in failures:
        print(failure)
    if not failures:
        print("Architecture OK: FLLBot core and plugin package boundaries.")
    raise SystemExit(int(bool(failures)))
