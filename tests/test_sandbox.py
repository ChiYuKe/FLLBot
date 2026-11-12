from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.onebot.models import parse_event
from fllbot.plugin.base import AIToolSpec
from fllbot.plugin.context import Context
from fllbot.sandbox import (
    CapabilityError,
    SandboxError,
    SandboxFiles,
    SandboxNetwork,
    effective_risk,
    normalize,
    resolve,
)
from fllbot.sandbox.guard import decide
from helpers import FakeConnection, build_bot, group_message, make_bot

AUTH = {"Authorization": "Bearer admin-token"}


def policy(tmp_path: Path, **overrides):
    settings = {
        "enabled": True,
        "workspace": str(tmp_path / "workspace"),
        "allow_read": True,
        "allow_write": False,
        "allow_network": True,
        "allow_network_write": False,
        "allow_private_network": False,
        "allowed_hosts": [],
        "denied_hosts": [],
        "deny_paths": [".env", "config.toml", "data/admin_token.txt"],
        "max_read_bytes": 32,
        "max_write_bytes": 32,
        "max_fetch_bytes": 64,
        "max_calls_per_turn": 3,
        "max_write_calls_per_turn": 1,
    }
    settings.update(overrides)
    return resolve(settings, tmp_path)


def test_capability_and_risk_validation():
    assert normalize(["net.read", "net.read", "net.read"]) == ("net.read",)
    with pytest.raises(CapabilityError):
        normalize(["net.teleport"])
    assert effective_risk("read_only", ("fs.read",)) == "read"
    assert effective_risk("side_effecting", ("fs.write",)) == "write"
    with pytest.raises(CapabilityError):
        effective_risk("read_only", ("fs.write",))
    with pytest.raises(ValueError):
        AIToolSpec("bad", "", {"type": "object"}, _noop, effect="read_only",
                   capabilities=("qq.manage",), risk="read")


async def _noop(ctx, **kwargs):
    return {}


def test_guard_denies_capability_path_url_and_authority(tmp_path):
    read = policy(tmp_path)
    verdict = decide(tool="t", spec_capabilities=("net.read",), spec_risk="read",
                     arguments={"url": "https://example.com/a"}, policy=read,
                     addressed=False, trigger_kind="automatic", superuser=False,
                     group_admin=False)
    assert verdict.allowed

    disabled_write = decide(tool="t", spec_capabilities=("fs.write",), spec_risk="write",
                            arguments={"path": "a.txt"}, policy=read,
                            addressed=True, trigger_kind="at", superuser=False,
                            group_admin=False)
    assert (disabled_write.allowed, disabled_write.reason) == (False, "write_disabled")

    writable = policy(tmp_path, allow_write=True)
    automatic = decide(tool="t", spec_capabilities=("fs.write",), spec_risk="write",
                       arguments={"path": "a.txt"}, policy=writable,
                       addressed=False, trigger_kind="automatic", superuser=False,
                       group_admin=False)
    assert (automatic.allowed, automatic.reason) == (False, "automatic_side_effect")

    for path in ("../secret.txt", "C:\\Windows\\win.ini", "/etc/passwd", "~/.ssh/id_rsa",
                 "notes/../../outside.txt"):
        verdict = decide(tool="t", spec_capabilities=("fs.read",), spec_risk="read",
                         arguments={"path": path}, policy=read, addressed=True,
                         trigger_kind="at", superuser=False, group_admin=False)
        assert not verdict.allowed, path

    denied = decide(tool="t", spec_capabilities=("fs.read",), spec_risk="read",
                    arguments={"file": ".env"}, policy=read, addressed=True,
                    trigger_kind="at", superuser=False, group_admin=False)
    assert (denied.allowed, denied.reason) == (False, "path_denied")

    private = decide(tool="t", spec_capabilities=("net.read",), spec_risk="read",
                     arguments={"url": "http://127.0.0.1:8080/a"}, policy=read,
                     addressed=True, trigger_kind="at", superuser=False, group_admin=False)
    assert (private.allowed, private.reason) == (False, "network_private_denied")
    metadata = decide(tool="t", spec_capabilities=("net.read",), spec_risk="read",
                      arguments={"url": "http://169.254.169.254/latest/meta-data"}, policy=read,
                      addressed=True, trigger_kind="at", superuser=False, group_admin=False)
    assert (metadata.allowed, metadata.reason) == (False, "network_private_denied")

    allowlisted = policy(tmp_path, allowed_hosts=["example.com"])
    assert decide(tool="t", spec_capabilities=("net.read",), spec_risk="read",
                  arguments={"url": "https://example.com/x"}, policy=allowlisted,
                  addressed=True, trigger_kind="at", superuser=False, group_admin=False).allowed
    blocked = decide(tool="t", spec_capabilities=("net.read",), spec_risk="read",
                     arguments={"url": "https://other.example.org/x"}, policy=allowlisted,
                     addressed=True, trigger_kind="at", superuser=False, group_admin=False)
    assert (blocked.allowed, blocked.reason) == (False, "network_host_not_allowed")

    manage = decide(tool="t", spec_capabilities=("qq.manage",), spec_risk="destructive",
                    arguments={}, policy=policy(tmp_path, allow_destructive=True),
                    addressed=True, trigger_kind="at", superuser=False, group_admin=False)
    assert (manage.allowed, manage.reason) == (False, "authority_required")


def test_guard_quota_and_legacy_tools(tmp_path):
    current = policy(tmp_path)
    allowed = decide(tool="t", spec_capabilities=("net.read",), spec_risk="read",
                     arguments={}, policy=current, addressed=True, trigger_kind="at",
                     superuser=False, group_admin=False, usage={"calls": 3})
    assert (allowed.allowed, allowed.reason) == (False, "turn_quota_exceeded")
    legacy = decide(tool="t", spec_capabilities=(), spec_risk="read", arguments={},
                    policy=policy(tmp_path, enabled=False), addressed=False,
                    trigger_kind="automatic", superuser=False, group_admin=False)
    assert legacy.allowed and legacy.reason == "sandbox_disabled"


def test_files_are_confined_and_bounded(tmp_path):
    settings = policy(tmp_path, allow_write=True)
    files = SandboxFiles(settings)
    written = files.write_text("notes/a.txt", "hello")
    assert written["path"] == "notes/a.txt"
    assert files.read_text("notes/a.txt")["content"] == "hello"
    assert (tmp_path / "workspace" / "notes" / "a.txt").read_text(encoding="utf-8") == "hello"
    listing = files.list_dir("notes")
    assert [item["name"] for item in listing["entries"]] == ["a.txt"]
    trimmed = files.read_text("notes/a.txt", max_bytes=2)
    assert trimmed["truncated"] and trimmed["content"] == "he"
    for path in ("../x.txt", "/etc/passwd", "C:\\Windows\\win.ini"):
        with pytest.raises(SandboxError):
            files.read_text(path)
    with pytest.raises(SandboxError):
        files.write_text(".env", "secret")
    with pytest.raises(SandboxError):
        files.write_text("big.txt", "x" * 64)
    with pytest.raises(SandboxError):
        SandboxFiles(policy(tmp_path)).write_text("a.txt", "x")


async def test_network_revalidates_redirects_and_types(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/page"})
        if request.url.path == "/private":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        if request.url.path == "/binary":
            return httpx.Response(200, headers={"content-type": "application/octet-stream"},
                                  content=b"\x00")
        return httpx.Response(200, headers={"content-type": "text/plain; charset=utf-8"},
                              content=b"hello page")

    transport = httpx.MockTransport(handler)
    def resolver(host, port):
        return ["10.0.0.5"] if host == "private.example.com" else ["93.184.216.34"]
    network = SandboxNetwork(policy(tmp_path), transport=transport, resolver=resolver)
    result = await network.fetch("http://example.com/start")
    assert result["text"] == "hello page" and result["url"].endswith("/page")
    assert seen == ["http://example.com/start", "http://example.com/page"]

    redirect = SandboxNetwork(policy(tmp_path), transport=transport, resolver=resolver)
    with pytest.raises(SandboxError):
        await redirect.fetch("http://example.com/private")
    private_dns = SandboxNetwork(policy(tmp_path), transport=transport, resolver=resolver)
    with pytest.raises(SandboxError):
        await private_dns.fetch("http://private.example.com/a")
    content = SandboxNetwork(policy(tmp_path), transport=transport, resolver=resolver)
    with pytest.raises(SandboxError):
        await content.fetch("http://example.com/binary")
    with pytest.raises(SandboxError):
        await network.fetch("http://127.0.0.1/a")
    with pytest.raises(SandboxError):
        await network.fetch("ftp://example.com/a")
    with pytest.raises(SandboxError):
        await network.fetch("http://user:pass@example.com/a")


@pytest.mark.parametrize("change", ["write_disabled", "automatic", "denied_path", "quota"])
async def test_tool_executor_blocks_before_callback(tmp_path, change):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        called = []

        async def callback(ctx, **arguments):
            called.append(arguments)
            return {"ok": True}

        spec = AIToolSpec(
            "write", "", {"type": "object", "properties": {"path": {"type": "string"}},
                          "additionalProperties": False},
            callback, plugin_name="example", effect="side_effecting",
            capabilities=("fs.write",),
        )
        ctx = Context(bot, FakeConnection(), parse_event(group_message("hello")))
        ctx.trigger_decision = SimpleNamespace(trigger_kind="at")
        registry = {spec.qualified_name: (SimpleNamespace(name="example"), spec)}
        arguments = {"path": "a.txt"}
        if change == "write_disabled":
            pass  # 默认 allow_write=false
        elif change == "automatic":
            ctx.trigger_decision = SimpleNamespace(trigger_kind="automatic")
            await bot.update_config({"sandbox": {"allow_write": True}})
        elif change == "denied_path":
            await bot.update_config({"sandbox": {"allow_write": True}})
            arguments = {"path": ".env"}
        else:
            await bot.update_config({"sandbox": {"allow_write": True, "max_calls_per_turn": 1}})
            ctx.sandbox_usage["calls"] = 1
            ctx.sandbox_usage["writes"] = 0
        output, _ = await bot.tool_executor.execute(ctx, spec.qualified_name, arguments, registry)
        assert not called
        payload = __import__("json").loads(output)
        assert payload["error"] == "tool_blocked"
        assert payload["reason"]
    finally:
        await bot.stop()


async def test_tool_executor_allows_and_counts_explicit_write(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    try:
        await bot.update_config({"sandbox": {"allow_write": True}})

        async def callback(ctx, path):
            return ctx.sandbox.files.write_text(path, "data")

        spec = AIToolSpec(
            "write", "", {"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"], "additionalProperties": False},
            callback, plugin_name="example", effect="side_effecting",
            capabilities=("fs.write",),
        )
        ctx = Context(bot, FakeConnection(), parse_event(group_message("hello")))
        ctx.trigger_decision = SimpleNamespace(trigger_kind="at")
        registry = {spec.qualified_name: (SimpleNamespace(name="example"), spec)}
        output, _ = await bot.tool_executor.execute(ctx, spec.qualified_name, {"path": "n/a.txt"}, registry)
        assert "n/a.txt" in output
        assert ctx.sandbox_usage["writes"] == 1
        assert (bot.sandbox.policy().workspace / "n" / "a.txt").read_text(encoding="utf-8") == "data"
        status = bot.sandbox.status()
        assert status["stats"]["allowed"] >= 1
        assert status["recent"][-1]["tool"] == "example__write"
    finally:
        await bot.stop()


def test_sandbox_api_is_protected_and_reports_policy(tmp_path):
    bot = build_bot(tmp_path)
    with TestClient(create_app(bot)) as client:
        assert client.get("/api/sandbox").status_code == 401
        data = client.get("/api/sandbox?group_id=20002", headers=AUTH).json()
        assert data["enabled"] is True
        assert data["capabilities"]["write"] is False
        assert data["limits"]["max_calls_per_turn"] == 16
        assert data["group"]["group_id"] == 20002


def test_group_sandbox_overrides_survive_partial_policy_edits(tmp_path):
    bot = build_bot(tmp_path)
    with TestClient(create_app(bot)) as client:
        payload = {"enabled": True, "policy": {
            "auto_group": True, "trigger_modes": ["all"],
            "participation_threshold": 5, "sandbox_write": True, "sandbox_network": False,
        }}
        assert client.patch("/api/console/groups/321", headers=AUTH, json=payload).status_code == 200
        assert bot.sandbox.policy(321).allow_write is True
        assert bot.sandbox.policy(321).allow_network is False
        assert client.patch("/api/console/groups/321", headers=AUTH, json={
            "enabled": True, "policy": {"cooldown_seconds": 0}}).status_code == 200
        policy = client.get("/api/console/groups/321", headers=AUTH).json()["policy"]
        assert policy["sandbox_write"] is True
        assert policy["sandbox_network"] is False
        assert policy["participation_threshold"] == 5
