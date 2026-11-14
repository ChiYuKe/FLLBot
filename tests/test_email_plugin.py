"""QQ 邮箱工具测试：地址校验、SMTP 发送与沙盒边界。"""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import smtplib
import sys
from email.header import decode_header, make_header
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fllbot.onebot.models import parse_event
from fllbot.onebot.segments import MessageSegment
from fllbot.plugin.context import Context
from fllbot.sandbox import effective_risk, resolve
from fllbot.sandbox.guard import decide
from helpers import FakeConnection, group_message, make_bot

ROOT = Path(__file__).resolve().parents[1]


def load_email_plugin() -> Any:
    """按宿主的方式导入插件包，返回 plugin.py 模块。"""
    package_name = "fllbot_test_email_plugin"
    if package_name in sys.modules:
        return sys.modules[package_name + ".plugin"]
    directory = ROOT / "plugins" / "email"
    spec = importlib.util.spec_from_file_location(
        package_name, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    assert spec and spec.loader
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    return sys.modules[package_name + ".plugin"]


email = load_email_plugin()
service = sys.modules["fllbot_test_email_plugin.service"]


def settings(**overrides: Any) -> dict[str, Any]:
    values = {
        "sender": "123456789@qq.com",
        "auth_code": "abcdefghijklmnop",
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "security": "ssl",
        "timeout": 15.0,
        "recipient_domains": ["qq.com"],
        "max_recipients": 5,
        "max_body_chars": 5000,
    }
    values.update(overrides)
    return values


def test_plugin_declares_email_tool() -> None:
    assert email.plugin.name == "email"
    specs = {spec.name: spec for spec in email.plugin.ai_tools}
    assert set(specs) == {"send_email"}
    spec = specs["send_email"]
    assert spec.plugin_name == "email"
    assert spec.title == "发送邮件"
    assert spec.effect == "side_effecting"
    assert spec.capabilities == ("email.send",)
    assert spec.risk == "write"
    assert spec.parameters["required"] == ["subject", "body"]
    assert {item.name for item in email.plugin.agent_instructions} == {"send_email"}


def test_parse_recipients_accepts_qq_numbers_and_qq_mail() -> None:
    assert service.parse_recipients("123456", allowed_domains=("qq.com",)) == ["123456@qq.com"]
    assert service.parse_recipients("123456, 654321@qq.com；ABC@QQ.com", allowed_domains=("qq.com",)) == [
        "123456@qq.com",
        "654321@qq.com",
        "ABC@qq.com",
    ]
    assert service.parse_recipients("", default_qq=30003, allowed_domains=("qq.com",)) == ["30003@qq.com"]
    assert service.parse_recipients("123456 123456@qq.com", allowed_domains=("qq.com",)) == ["123456@qq.com"]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("", "email_recipient_missing"),
        ("123", "email_recipient_invalid"),
        ("not-an-email", "email_recipient_invalid"),
        ("a@example.com", "email_recipient_domain_denied"),
        ("123456@qq.com, 2@qq.com, 3@qq.com, 4@qq.com, 5@qq.com, 6@qq.com", "email_too_many_recipients"),
    ],
)
def test_parse_recipients_rejects_invalid_values(value: str, code: str) -> None:
    with pytest.raises(service.EmailError) as excinfo:
        service.parse_recipients(value, max_recipients=5, allowed_domains=("qq.com",))
    assert excinfo.value.code == code


def test_parse_subject_and_body() -> None:
    assert service.parse_subject("  周报  ") == "周报"
    with pytest.raises(service.EmailError):
        service.parse_subject("")
    with pytest.raises(service.EmailError):
        service.parse_subject("第一行\n第二行")
    assert service.parse_body("第一行\r\n第二行") == "第一行\n第二行"
    with pytest.raises(service.EmailError):
        service.parse_body("   ")
    with pytest.raises(service.EmailError):
        service.parse_body("x" * 11, max_chars=10)


def test_validate_config_requires_sender_and_auth_code() -> None:
    with pytest.raises(service.EmailError) as excinfo:
        service.validate_config(
            sender="", auth_code="", smtp_host="smtp.qq.com", smtp_port=465,
            security="ssl", timeout=15,
        )
    assert excinfo.value.code == "email_not_configured"
    with pytest.raises(service.EmailError) as excinfo:
        service.validate_config(
            sender="123456@qq.com", auth_code="", smtp_host="smtp.qq.com", smtp_port=465,
            security="ssl", timeout=15,
        )
    assert excinfo.value.code == "email_not_configured"
    config = service.validate_config(
        sender="123456@qq.com", auth_code="a b c", smtp_host="SMTP.QQ.COM", smtp_port=465,
        security="ssl", timeout=0,
    )
    assert config["sender"] == "123456@qq.com"
    assert config["auth_code"] == "abc"
    assert config["smtp_host"] == "smtp.qq.com"
    assert config["timeout"] == 5.0


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float = 0, context: Any = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.logged_in: tuple[str, str] | None = None
        self.message: Any = None
        self.closed = False
        FakeSMTP.instances.append(self)

    def ehlo(self) -> None:
        return None

    def starttls(self, *, context: Any = None) -> None:
        self.context = context

    def login(self, sender: str, auth_code: str) -> None:
        self.logged_in = (sender, auth_code)

    def send_message(self, message: Any) -> dict[str, Any]:
        self.message = message
        return {}

    def quit(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def test_send_email_builds_utf8_message(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    result = service.send_email(
        sender="123456@qq.com",
        auth_code="secret-code",
        recipients=["654321@qq.com"],
        subject="测试主题",
        body="第一行\n第二行",
    )
    assert result["sent"] is True
    smtp = FakeSMTP.instances[0]
    assert smtp.logged_in == ("123456@qq.com", "secret-code")
    assert smtp.host == "smtp.qq.com" and smtp.port == 465
    message = smtp.message
    assert message["To"] == "654321@qq.com"
    assert str(make_header(decode_header(message["Subject"]))) == "测试主题"
    assert message.get_content_type() == "text/plain"
    assert message.get_content_charset() == "utf-8"
    assert message.get_payload(decode=True).decode("utf-8").rstrip("\n") == "第一行\n第二行"


def test_send_email_maps_auth_failure_without_leaking_code(monkeypatch: pytest.MonkeyPatch) -> None:
    class BadSMTP(FakeSMTP):
        def login(self, sender: str, auth_code: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtplib, "SMTP_SSL", BadSMTP)
    with pytest.raises(service.EmailError) as excinfo:
        service.send_email(
            sender="123456@qq.com", auth_code="secret-code", recipients=["654321@qq.com"],
            subject="主题", body="正文",
        )
    assert excinfo.value.code == "email_auth_failed"
    assert "secret-code" not in str(excinfo.value)


async def test_tool_defaults_to_requester_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_send(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"sent": True, "recipients": kwargs["recipients"]}

    monkeypatch.setattr(email, "send_email", fake_send)
    ctx = SimpleNamespace(
        settings=settings(),
        user_id=30003,
        text="把周报发到我的邮箱",
        raw_text="把周报发到我的邮箱",
        message=[],
        logger=logging.getLogger("test-email"),
    )
    result = await email.send_email_tool(ctx, subject="周报", body="本周完成……")
    assert result["sent"] is True
    assert result["to"] == ["30003@qq.com"]
    assert captured["recipients"] == ["30003@qq.com"]
    assert captured["subject"] == "周报"
    assert captured["body"] == "本周完成……"
    assert captured["auth_code"] == "abcdefghijklmnop"


async def test_explicit_recipient_must_appear_in_current_message(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[dict[str, Any]] = []
    monkeypatch.setattr(email, "send_email", lambda **kwargs: called.append(kwargs) or {"sent": True})
    ctx = SimpleNamespace(
        settings=settings(),
        user_id=953048766,
        text="把你想要对我说的话发到我的邮箱",
        raw_text="把你想要对我说的话发到我的邮箱",
        message=[],
        logger=logging.getLogger("test-email"),
    )

    result = await email.send_email_tool(
        ctx, subject="悄悄话", body="只给你一个人看", to="2902599249"
    )

    assert result["error"] == "email_recipient_not_requested"
    assert not called


async def test_explicit_recipient_from_message_or_mention_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def fake_send(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"sent": True, "recipients": kwargs["recipients"]}

    monkeypatch.setattr(email, "send_email", fake_send)
    ctx = SimpleNamespace(
        settings=settings(),
        user_id=953048766,
        text="把悄悄话发给 2902599249 和 953048766@qq.com",
        raw_text="把悄悄话发给 2902599249 和 953048766@qq.com",
        message=[],
        logger=logging.getLogger("test-email"),
    )
    result = await email.send_email_tool(
        ctx, subject="悄悄话", body="正文", to="2902599249, 953048766"
    )
    assert result["sent"] is True
    assert captured[0]["recipients"] == ["2902599249@qq.com", "953048766@qq.com"]

    mentioned = SimpleNamespace(
        settings=settings(),
        user_id=953048766,
        text="用邮件把悄悄话发给他",
        raw_text="用邮件把悄悄话发给他",
        message=[MessageSegment.at(2902599249)],
        logger=logging.getLogger("test-email"),
    )
    result = await email.send_email_tool(mentioned, subject="悄悄话", body="正文", to="2902599249")
    assert result["sent"] is True
    assert captured[1]["recipients"] == ["2902599249@qq.com"]

    own = SimpleNamespace(
        settings=settings(),
        user_id=953048766,
        text="用邮件把悄悄话发给我",
        raw_text="用邮件把悄悄话发给我",
        message=[],
        logger=logging.getLogger("test-email"),
    )
    result = await email.send_email_tool(own, subject="悄悄话", body="正文", to="953048766")
    assert result["sent"] is True
    assert captured[2]["recipients"] == ["953048766@qq.com"]


async def test_tool_requires_an_email_request_in_the_current_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[dict[str, Any]] = []
    monkeypatch.setattr(email, "send_email", lambda **kwargs: called.append(kwargs) or {"sent": True})
    ctx = SimpleNamespace(
        settings=settings(),
        user_id=953048766,
        text="妈妈",
        raw_text="妈妈",
        message=[],
        logger=logging.getLogger("test-email"),
    )
    result = await email.send_email_tool(ctx, subject="悄悄话", body="只给你一个人看")
    assert result["error"] == "email_request_missing"
    result = await email.send_email_tool(
        ctx, subject="悄悄话", body="只给你一个人看", to="2902599249"
    )
    assert result["error"] == "email_request_missing"
    assert not called


async def test_tool_reports_missing_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    monkeypatch.setattr(email, "send_email", lambda **kwargs: called.append(kwargs) or {"sent": True})
    ctx = SimpleNamespace(settings=settings(sender="", auth_code=""), user_id=30003,
                          logger=logging.getLogger("test-email"))
    result = await email.send_email_tool(ctx, subject="周报", body="正文")
    assert result["error"] == "email_not_configured"
    assert "发件邮箱" in result["message"]
    assert not called


def test_email_capability_requires_opt_in_and_explicit_request(tmp_path: Path) -> None:
    assert effective_risk("side_effecting", ("email.send",)) == "write"
    disabled = decide(
        tool="email", spec_capabilities=("email.send",), spec_risk="write", arguments={},
        policy=resolve({"allow_email": False}, tmp_path), addressed=True, trigger_kind="at",
        superuser=True, group_admin=True,
    )
    assert (disabled.allowed, disabled.reason) == (False, "email_disabled")

    policy = resolve({"allow_email": True}, tmp_path)
    automatic = decide(
        tool="email", spec_capabilities=("email.send",), spec_risk="write", arguments={},
        policy=policy, addressed=False, trigger_kind="automatic", superuser=False, group_admin=False,
    )
    assert (automatic.allowed, automatic.reason) == (False, "automatic_side_effect")
    allowed = decide(
        tool="email", spec_capabilities=("email.send",), spec_risk="write", arguments={},
        policy=policy, addressed=True, trigger_kind="at", superuser=False, group_admin=False,
    )
    assert allowed.allowed
    offline = decide(
        tool="email", spec_capabilities=("email.send",), spec_risk="write", arguments={},
        policy=resolve({"allow_email": True, "allow_network": False}, tmp_path),
        addressed=True, trigger_kind="at", superuser=False, group_admin=False,
    )
    assert (offline.allowed, offline.reason) == (False, "network_disabled")


async def test_plugin_is_loaded_and_sandboxed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    shutil.copytree(ROOT / "plugins" / "email", plugin_dir / "email")
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir])
    try:
        installed = bot.plugins.get("email")
        assert installed is not None
        spec = next(tool for tool in installed.ai_tools if tool.name == "send_email")
        await installed.settings.update({"sender": "123456789@qq.com", "auth_code": "secret-code"})
        module = sys.modules[installed.module.__name__ + ".plugin"]
        sent: list[dict[str, Any]] = []

        def fake_send(**kwargs: Any) -> dict[str, Any]:
            sent.append(kwargs)
            return {"sent": True, "recipients": kwargs["recipients"]}

        monkeypatch.setattr(module, "send_email", fake_send)
        ctx = Context(bot, FakeConnection(), parse_event(group_message("把总结发我邮箱", user_id=30003)))
        ctx.trigger_decision = SimpleNamespace(trigger_kind="at", addressed=True)
        registry = {spec.qualified_name: (installed, spec)}

        output, _ = await bot.tool_executor.execute(
            ctx, spec.qualified_name, {"subject": "总结", "body": "正文"}, registry
        )
        payload = json.loads(output)
        assert payload["error"] == "tool_blocked"
        assert payload["reason"] == "email_disabled"
        assert not sent

        await bot.update_config({"sandbox": {"allow_email": True}})
        output, _ = await bot.tool_executor.execute(
            ctx, spec.qualified_name, {"subject": "总结", "body": "正文"}, registry
        )
        payload = json.loads(output)
        assert payload["sent"] is True
        assert payload["to"] == ["30003@qq.com"]
        assert sent[0]["recipients"] == ["30003@qq.com"]
    finally:
        await bot.stop()
