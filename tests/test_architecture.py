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


def test_model_gateway_cannot_depend_on_chat_or_delivery(tmp_path):
    path = tmp_path / 'fllbot/llm/model_gateway.py'
    path.parent.mkdir(parents=True)
    path.write_text('from .chat import ChatService\nfrom .output import send_chat_reply\n',encoding='utf-8')
    errors = CHECK(tmp_path)
    assert any('llm.chat' in error for error in errors)
    assert any('llm.output' in error for error in errors)


def test_context_assembler_cannot_depend_on_model_execution(tmp_path):
    path = tmp_path / 'fllbot/llm/context_assembly.py'
    path.parent.mkdir(parents=True)
    path.write_text('from .model_gateway import ModelGateway\n',encoding='utf-8')
    assert CHECK(tmp_path)


def test_delivery_cannot_import_model_gateway(tmp_path):
    path = tmp_path / 'fllbot/delivery_service.py'
    path.parent.mkdir(parents=True)
    path.write_text('from .llm.model_gateway import ModelGateway\n',encoding='utf-8')
    assert CHECK(tmp_path)


def test_agent_adapter_cannot_access_storage(tmp_path):
    path = tmp_path / 'fllbot/builtin_plugins/agent/plugin.py'
    path.parent.mkdir(parents=True)
    path.write_text('async def respond(ctx):\n    await ctx.db.kv_get("key", "value")\n',encoding='utf-8')
    assert CHECK(tmp_path)
