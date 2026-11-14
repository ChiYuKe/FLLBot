import base64
import importlib
from types import SimpleNamespace

import httpx

from fllbot.sandbox import SandboxFiles, SandboxNetwork, resolve

module = importlib.import_module("fllbot.builtin_plugins.workspace.plugin")


def _ctx(tmp_path, **settings):
    policy = resolve({
        "workspace": str(tmp_path / "workspace"),
        "allow_read": True,
        "allow_write": True,
        "max_file_bytes": 1000,
        **settings,
    }, tmp_path)
    files = SandboxFiles(policy)
    return SimpleNamespace(sandbox=SimpleNamespace(files=files)), files


async def test_send_file_sends_workspace_binary_as_attachment(tmp_path, monkeypatch):
    sent = []

    async def sender(ctx, segments, content, *, require_ai=True):
        sent.append((segments, content, require_ai))
        return True

    monkeypatch.setattr(module, "send_media_reply", sender)
    ctx, files = _ctx(tmp_path)
    files.write_bytes("dist/app.py", b"print('hello')\n")
    result = await module.send_file(ctx, "dist/app.py")
    assert result["sent"] is True and result["filename"] == "app.py"
    segments, content, require_ai = sent[0]
    assert segments[0].type == "file" and segments[0].data["name"] == "app.py"
    raw = str(segments[0].data["file"]).removeprefix("base64://")
    assert base64.b64decode(raw) == b"print('hello')\n"
    assert content == "（文件：app.py）" and require_ai is True

    renamed = await module.send_file(ctx, "dist/app.py", "renamed.py")
    assert renamed["filename"] == "renamed.py"


async def test_send_file_rejects_dangerous_types_and_missing_files(tmp_path, monkeypatch):
    async def sender(ctx, segments, content, *, require_ai=True):
        return True

    monkeypatch.setattr(module, "send_media_reply", sender)
    ctx, files = _ctx(tmp_path)
    files.write_bytes("dist/tool.exe", b"MZ")
    denied = await module.send_file(ctx, "dist/tool.exe")
    assert denied["error"] == "file_type_denied"
    missing = await module.send_file(ctx, "dist/missing.py")
    assert missing["error"] == "not_found"


async def test_download_saves_binary_and_rejects_oversized(tmp_path):
    policy = resolve({
        "workspace": str(tmp_path / "workspace"), "allow_read": True, "allow_write": True,
        "allow_network": True, "max_file_bytes": 8, "max_fetch_bytes": 100,
    }, tmp_path)
    files = SandboxFiles(policy)

    def handler(request):
        if request.url.path == "/big":
            return httpx.Response(200, headers={"content-type": "application/octet-stream"},
                                  content=b"0123456789")
        return httpx.Response(200, headers={"content-type": "application/octet-stream"},
                              content=b"\x00\x01binary")

    network = SandboxNetwork(policy, transport=httpx.MockTransport(handler),
                             resolver=lambda host, port: ["93.184.216.34"])
    ctx = SimpleNamespace(sandbox=SimpleNamespace(policy=policy, files=files, network=network))
    saved = await module.download(ctx, "http://example.com/app.bin", "assets/app.bin")
    assert saved["saved"] is True and saved["bytes"] == 8
    assert (tmp_path / "workspace" / "assets" / "app.bin").read_bytes() == b"\x00\x01binary"
    oversized = await module.download(ctx, "http://example.com/big", "assets/big.bin")
    assert oversized["error"] == "download_too_large"
