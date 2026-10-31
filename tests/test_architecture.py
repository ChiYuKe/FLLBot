"""Runtime dependency boundaries, independent of annotations."""

import runpy
from pathlib import Path

CHECK = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check_architecture.py"))["check"]


def test_project_boundaries():
    assert CHECK() == []


def test_protocol_cannot_import_application_or_plugin(tmp_path):
    path = tmp_path / "fllbot/onebot/models.py"
    path.parent.mkdir(parents=True)
    path.write_text("def local():\n    from fllbot.plugin import Plugin\n", encoding="utf-8")
    assert CHECK(tmp_path)


def test_type_only_app_reference_is_allowed(tmp_path):
    path = tmp_path / "fllbot/plugin/context.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from fllbot.app import FLLBot\n", encoding="utf-8"
    )
    assert CHECK(tmp_path) == []


def test_plugins_cannot_import_another_plugin_package(tmp_path):
    path = tmp_path / "plugins/demo/handlers.py"
    path.parent.mkdir(parents=True)
    path.write_text("from ..other import plugin\n", encoding="utf-8")
    assert CHECK(tmp_path)
