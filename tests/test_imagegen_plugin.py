"""图片生成插件测试（httpx.MockTransport，不访问网络）。"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import random
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from fllbot.onebot.models import parse_event
from fllbot.plugin import Context
from fllbot.plugin.agent import instruction_sections
from helpers import FakeConnection, group_message, make_bot

ROOT = Path(__file__).resolve().parents[1]


def load_plugin_package(name: str) -> Any:
    """按宿主的方式导入外置插件包，返回 plugin.py 模块。"""
    package_name = f"fllbot_test_{name}_plugin"
    if package_name in sys.modules:
        return sys.modules[package_name + ".plugin"]
    directory = ROOT / "plugins" / name
    spec = importlib.util.spec_from_file_location(
        package_name, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    assert spec and spec.loader
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    return sys.modules[package_name + ".plugin"]


imagegen = load_plugin_package("imagegen")
service = sys.modules["fllbot_test_imagegen_plugin.service"]
local = sys.modules["fllbot_test_imagegen_plugin.local"]


def make_png(width: int = 128, height: int = 128) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 160, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


PNG = make_png()
PNG_B64 = base64.b64encode(PNG).decode("ascii")


@pytest.fixture(autouse=True)
def reset_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imagegen, "_LAST", {})


def use_transport(monkeypatch: pytest.MonkeyPatch, handler: Any, module: Any = None) -> None:
    """把插件的 HTTP 客户端换成 MockTransport；加载后的插件包有独立的 service 模块。"""
    target = module or service

    def _new_client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(target, "_new_client", _new_client)


def use_bot_transport(monkeypatch: pytest.MonkeyPatch, installed: Any, handler: Any) -> None:
    use_transport(monkeypatch, handler, sys.modules[installed.module.__name__ + ".service"])


def engine(**overrides: Any) -> Any:
    options = {
        "provider": "openai",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
        "model": "gpt-image-1",
        "size": "1024x1024",
    }
    options.update(overrides)
    return service.validate_engine(**options)


async def test_openai_generate_returns_png(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/generations"
        assert request.headers["authorization"] == "Bearer sk-test"
        body = json.loads(request.content)
        assert body["model"] == "gpt-image-1"
        assert body["prompt"] == "一只银灰色猫娘"
        assert body["size"] == "1024x1024"
        assert body["n"] == 1
        return httpx.Response(200, json={"data": [{"b64_json": PNG_B64}]})

    use_transport(monkeypatch, handler)
    result = await service.generate(engine(), "一只银灰色猫娘")
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == "PNG"
        assert image.size == (128, 128)


async def test_openai_url_response_is_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/images/generations":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example.com/a.png"}]})
        assert request.url.host == "cdn.example.com"
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    use_transport(monkeypatch, handler)
    result = await service.generate(engine(), "风景")
    assert result.startswith(b"\x89PNG")


async def test_url_string_candidates_are_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"images": ["https://cdn.example.com/c.png"]})
        assert request.url.host == "cdn.example.com"
        return httpx.Response(200, content=PNG)

    use_transport(monkeypatch, handler)
    result = await service.generate(engine(), "猫")
    assert result.startswith(b"\x89PNG")


async def test_siliconflow_uses_image_size_and_negative_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        body = json.loads(request.content)
        assert body["image_size"] == "768x1024"
        assert body["num_inference_steps"] == 30
        assert body["negative_prompt"] == "低质量, 多余手指"
        assert "size" not in body
        return httpx.Response(200, json={"images": [{"url": "https://cdn.example.com/b.png"}]})

    use_transport(monkeypatch, handler)
    result = await service.generate(
        engine(provider="siliconflow", model="Kwai-Kolors/Kolors", steps=30),
        "猫猫，雨夜",
        "768x1024",
        "低质量, 多余手指",
    )
    assert result.startswith(b"\x89PNG")


async def test_sdwebui_payload_and_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sdapi/v1/txt2img"
        body = json.loads(request.content)
        assert body["width"] == 768 and body["height"] == 512
        assert body["steps"] == 24
        assert body["negative_prompt"] == "低质量"
        return httpx.Response(200, json={"images": [PNG_B64]})

    use_transport(monkeypatch, handler)
    result = await service.generate(
        engine(
            provider="sdwebui",
            base_url="http://127.0.0.1:7860",
            api_key="",
            model="",
            steps=24,
            negative_prompt="低质量",
        ),
        "小房子",
        "768x512",
    )
    assert result.startswith(b"\x89PNG")


async def test_http_error_does_not_leak_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key sk-secret"}})

    use_transport(monkeypatch, handler)
    with pytest.raises(service.ImageGenError) as caught:
        await service.generate(engine(api_key="sk-secret"), "猫")
    assert caught.value.code == "imagegen_http_error"
    assert "sk-secret" not in str(caught.value)


async def test_invalid_image_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]})

    use_transport(monkeypatch, handler)
    with pytest.raises(service.ImageGenError) as caught:
        await service.generate(engine(), "猫")
    assert caught.value.code == "imagegen_invalid_image"


async def test_timeout_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    use_transport(monkeypatch, handler)
    with pytest.raises(service.ImageGenError) as caught:
        await service.generate(engine(), "猫")
    assert caught.value.code == "imagegen_timeout"


async def test_oversized_image_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    noise = Image.frombytes("RGB", (512, 512), random.Random(42).randbytes(512 * 512 * 3))
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": payload}]})

    use_transport(monkeypatch, handler)
    with pytest.raises(service.ImageGenError) as caught:
        await service.generate(engine(max_bytes=256 * 1024), "猫")
    assert caught.value.code == "imagegen_too_large"


def test_size_must_be_multiple_of_eight() -> None:
    with pytest.raises(service.ImageGenError) as caught:
        service.parse_size("999x999", "1024x1024")
    assert caught.value.code == "imagegen_size_invalid"
    assert service.parse_size("768x1024", "1024x1024") == (768, 1024)


def test_local_render_returns_png() -> None:
    data = local.render(expression="happy", palette="night", seed=1, size=512)
    assert data.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.size == (512, 512)


def test_local_render_seed_is_stable() -> None:
    first = local.render(expression="happy", palette="sakura", seed=7, size=320)
    second = local.render(expression="happy", palette="sakura", seed=7, size=320)
    third = local.render(expression="happy", palette="sakura", seed=8, size=320)
    assert first == second
    assert first != third


def test_local_expressions_and_palettes_differ() -> None:
    happy = local.render(expression="happy", palette="night", seed=3, size=320)
    angry = local.render(expression="angry", palette="night", seed=3, size=320)
    sakura = local.render(expression="happy", palette="sakura", seed=3, size=320)
    assert happy != angry
    assert happy != sakura


def test_analyze_prompt_picks_expression_and_palette() -> None:
    assert local.analyze_prompt("生气地鼓着腮帮子") == ("angry", "night")
    assert local.analyze_prompt("雨夜，委屈巴巴") == ("sad", "rain")
    assert local.analyze_prompt("樱花飘落，好开心") == ("happy", "sakura")
    assert local.analyze_prompt("随便画点什么") == ("happy", "night")


async def test_local_tool_draws_without_remote_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "imagegen", plugin_dir / "imagegen")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("imagegen")
        assert installed is not None
        await installed.settings.update({"provider": "local", "cooldown_seconds": 0})
        send_media = AsyncMock(return_value=True)
        monkeypatch.setattr(bot.deliveries, "send_media", send_media)
        spec = next(tool for tool in installed.ai_tools if tool.name == "draw")
        ctx = Context(
            bot=bot,
            connection=FakeConnection(),
            event=parse_event(group_message("画个生气的猫猫", at=10001)),
            plugin=installed,
            handler=spec,
        )

        result = await spec.callback(ctx, prompt="生气地鼓着腮帮子")

        assert result["sent"] is True
        assert result["provider"] == "local"
        assert result["expression"] == "angry"
        assert result["silent"] is True
        assert 0 < len(result["reply"]) <= 60
        assert "imagegen__draw" not in result["reply"]
        segments, _ = send_media.call_args.args[1:3]
        assert [segment.type for segment in segments] == ["image"]
    finally:
        await bot.stop()


async def test_local_command_draws_with_default_config(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "imagegen", plugin_dir / "imagegen")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("/画 雨夜 委屈")), connection)
        segments = connection.sent[-1][2]
        assert [segment.type for segment in segments] == ["image"]
        assert str(segments[0].data["file"]).startswith("base64://")
    finally:
        await bot.stop()


async def test_tool_sends_image_and_reports_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "imagegen", plugin_dir / "imagegen")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("imagegen")
        assert installed is not None
        await installed.settings.update(
            {
                "provider": "openai",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-test",
                "model": "gpt-image-1",
                "cooldown_seconds": 0,
            }
        )
        use_bot_transport(
            monkeypatch, installed, lambda request: httpx.Response(200, json={"data": [{"b64_json": PNG_B64}]})
        )
        send_media = AsyncMock(return_value=True)
        monkeypatch.setattr(bot.deliveries, "send_media", send_media)
        spec = next(tool for tool in installed.ai_tools if tool.name == "draw")
        ctx = Context(
            bot=bot,
            connection=FakeConnection(),
            event=parse_event(group_message("画一只猫", at=10001)),
            plugin=installed,
            handler=spec,
        )

        result = await spec.callback(ctx, prompt="一只银灰色猫娘")

        assert result["sent"] is True
        assert result["size"] == "1024x1024"
        assert result["silent"] is True
        assert result["reply"]
        segments, content = send_media.call_args.args[1:3]
        assert [segment.type for segment in segments] == ["image"]
        assert str(segments[0].data["file"]).startswith("base64://")
        assert content.startswith("[图片]")
    finally:
        await bot.stop()


async def test_tool_reports_missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "imagegen", plugin_dir / "imagegen")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("imagegen")
        assert installed is not None
        await installed.settings.update({"provider": "openai", "api_key": "", "cooldown_seconds": 0})
        send_media = AsyncMock(return_value=True)
        monkeypatch.setattr(bot.deliveries, "send_media", send_media)
        spec = next(tool for tool in installed.ai_tools if tool.name == "draw")
        ctx = Context(
            bot=bot,
            connection=FakeConnection(),
            event=parse_event(group_message("画一只猫", at=10001)),
            plugin=installed,
            handler=spec,
        )

        result = await spec.callback(ctx, prompt="猫")

        assert result["error"] == "imagegen_not_configured"
        assert "silent" not in result and "reply" not in result
        send_media.assert_not_awaited()
    finally:
        await bot.stop()


async def test_tool_cooldown_returns_retry_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "imagegen", plugin_dir / "imagegen")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("imagegen")
        assert installed is not None
        await installed.settings.update(
            {
                "provider": "openai",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-test",
                "model": "gpt-image-1",
                "cooldown_seconds": 60,
            }
        )
        use_bot_transport(
            monkeypatch, installed, lambda request: httpx.Response(200, json={"data": [{"b64_json": PNG_B64}]})
        )
        monkeypatch.setattr(bot.deliveries, "send_media", AsyncMock(return_value=True))
        spec = next(tool for tool in installed.ai_tools if tool.name == "draw")
        ctx = Context(
            bot=bot,
            connection=FakeConnection(),
            event=parse_event(group_message("画一只猫", at=10001)),
            plugin=installed,
            handler=spec,
        )

        first = await spec.callback(ctx, prompt="猫")
        second = await spec.callback(ctx, prompt="狗")

        assert first["sent"] is True
        assert second["error"] == "imagegen_cooldown"
        assert second["retry_after"] > 0
        assert "silent" not in second and "reply" not in second
    finally:
        await bot.stop()


async def test_tool_requires_addressed_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "imagegen", plugin_dir / "imagegen")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("imagegen")
        assert installed is not None
        send_media = AsyncMock(return_value=True)
        monkeypatch.setattr(bot.deliveries, "send_media", send_media)
        spec = next(tool for tool in installed.ai_tools if tool.name == "draw")
        ctx = Context(
            bot=bot,
            connection=FakeConnection(),
            event=parse_event(group_message("好想养猫")),
            plugin=installed,
            handler=spec,
        )
        ctx.trigger_decision = SimpleNamespace(addressed=False)

        result = await spec.callback(ctx, prompt="一只猫")

        assert result["error"] == "imagegen_addressed_required"
        assert "silent" not in result and "reply" not in result
        send_media.assert_not_awaited()
    finally:
        await bot.stop()


async def test_tool_not_delivered_is_not_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "imagegen", plugin_dir / "imagegen")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("imagegen")
        assert installed is not None
        await installed.settings.update({"provider": "local", "cooldown_seconds": 0})
        monkeypatch.setattr(bot.deliveries, "send_media", AsyncMock(return_value=False))
        spec = next(tool for tool in installed.ai_tools if tool.name == "draw")
        ctx = Context(
            bot=bot,
            connection=FakeConnection(),
            event=parse_event(group_message("画个猫猫", at=10001)),
            plugin=installed,
            handler=spec,
        )

        result = await spec.callback(ctx, prompt="猫猫")

        assert result["sent"] is False
        assert result["error"] == "imagegen_not_delivered"
        assert "silent" not in result and "reply" not in result
    finally:
        await bot.stop()


async def test_command_draw_sends_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "imagegen", plugin_dir / "imagegen")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("imagegen")
        assert installed is not None
        await installed.settings.update(
            {
                "provider": "openai",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-test",
                "model": "gpt-image-1",
                "cooldown_seconds": 0,
            }
        )
        use_bot_transport(
            monkeypatch, installed, lambda request: httpx.Response(200, json={"data": [{"b64_json": PNG_B64}]})
        )
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("/画 一只猫")), connection)

        segments = connection.sent[-1][2]
        assert [segment.type for segment in segments] == ["image"]
        assert str(segments[0].data["file"]).startswith("base64://")
    finally:
        await bot.stop()


async def test_command_draw_without_argument_shows_usage(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "imagegen", plugin_dir / "imagegen")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("/画")), connection)
        assert "用法" in connection.last_text()
    finally:
        await bot.stop()


def test_draw_tool_declares_send_capability() -> None:
    spec = next(tool for tool in imagegen.plugin.ai_tools if tool.name == "draw")
    assert spec.effect == "side_effecting"
    assert set(spec.capabilities) == {"qq.send"}
    assert spec.timeout >= 240


def test_agent_instruction_requires_draw_tool() -> None:
    registry = {spec.qualified_name: (imagegen.plugin, spec) for spec in imagegen.plugin.ai_tools}
    definitions = [{"type": "function", "function": {"name": spec.qualified_name}} for spec in imagegen.plugin.ai_tools]
    sections = instruction_sections(registry, definitions)
    assert sections and sections[0]["source"] == "imagegen__image_generation"
    assert instruction_sections(registry, []) == []
