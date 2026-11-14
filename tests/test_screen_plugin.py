import importlib
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from fllbot.onebot.models import parse_event
from fllbot.plugin.base import AIToolSpec
from fllbot.plugin.context import Context
from fllbot.sandbox import SandboxFiles, resolve
from fllbot.sandbox.guard import decide
from helpers import FakeConnection, group_message, make_bot

screen_module = importlib.import_module("fllbot.builtin_plugins.screen.plugin")
service = importlib.import_module("fllbot.builtin_plugins.screen.service")


def _png(width: int = 320, height: int = 200) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_capture_resizes_and_encodes(monkeypatch):
    image = Image.new("RGB", (4000, 2000), "white")
    monkeypatch.setattr(service.ImageGrab, "grab", lambda all_screens=False: image)
    result = service.capture(all_screens=True, max_width=1000, image_format="png")
    assert result["width"] == 1000 and result["height"] == 500
    assert result["format"] == "png"
    assert result["data"].startswith(b"\x89PNG")


def test_capture_reports_missing_desktop(monkeypatch):
    def fail(all_screens=False):
        raise OSError("no desktop")

    monkeypatch.setattr(service.ImageGrab, "grab", fail)
    with pytest.raises(service.CaptureError) as excinfo:
        service.capture()
    assert excinfo.value.code == "screenshot_failed"


async def test_capture_tool_sends_image(monkeypatch):
    async def sender(ctx, segments, content, *, require_ai=True):
        sender.calls.append((segments, content, require_ai))
        return True

    sender.calls = []
    monkeypatch.setattr(screen_module, "send_media_reply", sender)
    monkeypatch.setattr(screen_module, "capture",
                        lambda **kwargs: {"data": _png(), "format": "png",
                                          "width": 320, "height": 200, "bytes": len(_png())})
    ctx = SimpleNamespace(settings={})
    result = await screen_module.capture_screen(ctx)
    assert result["sent"] is True
    assert result["width"] == 320
    segments, content, require_ai = sender.calls[0]
    assert segments[0].type == "image"
    assert str(segments[0].data["file"]).startswith("base64://")
    assert content == "（屏幕截图）" and require_ai is True

    async def failed(ctx, segments, content, *, require_ai=True):
        return False

    monkeypatch.setattr(screen_module, "send_media_reply", failed)
    result = await screen_module.capture_screen(ctx)
    assert result["sent"] is False and result["error"] == "send_failed"


async def test_capture_file_writes_inside_workspace(tmp_path, monkeypatch):
    policy = resolve({
        "workspace": str(tmp_path / "workspace"), "allow_write": True,
        "max_write_bytes": 200_000,
    }, tmp_path)
    ctx = SimpleNamespace(settings={}, sandbox=SimpleNamespace(files=SandboxFiles(policy)))
    monkeypatch.setattr(screen_module, "capture",
                        lambda **kwargs: {"data": _png(), "format": "png",
                                          "width": 320, "height": 200, "bytes": 1})
    result = await screen_module.capture_screen_file(ctx, "shots/a.png")
    assert result["saved"] is True and result["path"] == "shots/a.png"
    assert (tmp_path / "workspace" / "shots" / "a.png").is_file()
    generated = await screen_module.capture_screen_file(ctx, "")
    assert generated["path"].startswith("screenshots/screen-")


def test_screen_capability_requires_opt_in_and_authority(tmp_path):
    policy = resolve({"allow_screen": False}, tmp_path)
    denied = decide(tool="screen", spec_capabilities=("screen.capture",), spec_risk="destructive",
                    arguments={}, policy=policy, addressed=True, trigger_kind="at",
                    superuser=True, group_admin=True)
    assert (denied.allowed, denied.reason) == (False, "screen_disabled")

    policy = resolve({"allow_screen": True}, tmp_path)
    member = decide(tool="screen", spec_capabilities=("screen.capture",), spec_risk="destructive",
                    arguments={}, policy=policy, addressed=True, trigger_kind="at",
                    superuser=False, group_admin=False)
    assert (member.allowed, member.reason) == (False, "authority_required")
    automatic = decide(tool="screen", spec_capabilities=("screen.capture",), spec_risk="destructive",
                       arguments={}, policy=policy, addressed=False, trigger_kind="automatic",
                       superuser=True, group_admin=True)
    assert (automatic.allowed, automatic.reason) == (False, "automatic_side_effect")
    allowed = decide(tool="screen", spec_capabilities=("screen.capture",), spec_risk="destructive",
                     arguments={}, policy=policy, addressed=True, trigger_kind="at",
                     superuser=True, group_admin=True)
    assert allowed.allowed

    # 即使管理员关闭“仅超级管理员”，普通群成员仍不能截屏。
    relaxed = resolve({"allow_screen": True, "destructive_requires_superuser": False}, tmp_path)
    member = decide(tool="screen", spec_capabilities=("screen.capture",), spec_risk="destructive",
                    arguments={}, policy=relaxed, addressed=True, trigger_kind="at",
                    superuser=False, group_admin=False)
    assert (member.allowed, member.reason) == (False, "authority_required")
    owner = decide(tool="screen", spec_capabilities=("screen.capture",), spec_risk="destructive",
                   arguments={}, policy=relaxed, addressed=True, trigger_kind="at",
                   superuser=False, group_admin=True)
    assert owner.allowed


@pytest.mark.parametrize("variant", ["disabled", "member", "automatic"])
async def test_tool_executor_blocks_screen_before_callback(tmp_path, variant):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        called = []

        async def callback(ctx):
            called.append(True)
            return {"ok": True}

        spec = AIToolSpec("capture", "", {"type": "object"}, callback,
                          plugin_name="screen", effect="side_effecting",
                          capabilities=("screen.capture",), risk="destructive")
        user = 99999 if variant != "member" else 30003
        ctx = Context(bot, FakeConnection(), parse_event(group_message("截个屏", user_id=user)))
        ctx.trigger_decision = SimpleNamespace(trigger_kind="at", addressed=True)
        if variant != "disabled":
            await bot.update_config({"sandbox": {"allow_screen": True}})
        if variant == "automatic":
            ctx.trigger_decision = SimpleNamespace(trigger_kind="automatic", addressed=False)
        registry = {spec.qualified_name: (SimpleNamespace(name="screen"), spec)}
        output, _ = await bot.tool_executor.execute(ctx, spec.qualified_name, {}, registry)
        payload = json.loads(output)
        assert payload["error"] == "tool_blocked"
        assert payload["reason"] == {"disabled": "screen_disabled", "member": "authority_required",
                                     "automatic": "automatic_side_effect"}[variant]
        assert not called
    finally:
        await bot.stop()


async def test_screen_plugin_tools_are_declared(tmp_path):
    bot = await make_bot(tmp_path)
    try:
        screen = bot.plugins.get("screen")
        assert screen is not None
        specs = {spec.qualified_name: spec for spec in screen.ai_tools}
        assert set(specs) == {"screen__capture_screen", "screen__capture_screen_file"}
        assert specs["screen__capture_screen"].risk == "destructive"
        assert "screen.capture" in specs["screen__capture_screen"].capabilities
        assert "fs.write" in specs["screen__capture_screen_file"].capabilities
    finally:
        await bot.stop()
