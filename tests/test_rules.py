"""规则匹配测试。"""

from __future__ import annotations

from types import SimpleNamespace

from fllbot.config import AppConfig
from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from fllbot.rules import all_rules, command, keyword, not_rule, regex, startswith, to_me
from helpers import group_message, private_message


def make_ctx(payload: dict, config: AppConfig | None = None) -> Context:
    event = parse_event(payload)
    stub = SimpleNamespace(config=config or AppConfig())
    return Context(bot=stub, connection=None, event=event)  # type: ignore[arg-type]


def test_command_with_default_prefix() -> None:
    ctx = make_ctx(group_message("/ping"))
    rule = command("ping")
    assert rule(ctx) is True  # type: ignore[operator]
    assert ctx.command == "ping"
    assert ctx.args == ""


def test_command_with_args() -> None:
    ctx = make_ctx(group_message("/echo hello world"))
    rule = command("echo")
    assert rule(ctx) is True  # type: ignore[operator]
    assert ctx.args == "hello world"
    assert ctx.argv == ["hello", "world"]


def test_command_requires_prefix_or_at() -> None:
    ctx = make_ctx(group_message("ping"))
    assert command("ping")(ctx) is False  # type: ignore[operator]


def test_command_after_at() -> None:
    ctx = make_ctx(group_message("ping", at=10001))
    assert command("ping")(ctx) is True  # type: ignore[operator]


def test_command_aliases_and_case() -> None:
    ctx = make_ctx(group_message("/PING"))
    assert command("ping", aliases=("在吗",))(ctx) is True  # type: ignore[operator]


def test_command_bare_when_prefix_empty() -> None:
    config = AppConfig.model_validate({"bot": {"command_prefix": [""]}})
    ctx = make_ctx(group_message("ping"), config)
    assert command("ping")(ctx) is True  # type: ignore[operator]


def test_command_not_matched_for_longer_text() -> None:
    ctx = make_ctx(group_message("/pingabc"))
    assert command("ping")(ctx) is False  # type: ignore[operator]


def test_command_in_private() -> None:
    ctx = make_ctx(private_message("/help"))
    assert command("help")(ctx) is True  # type: ignore[operator]


def test_keyword_and_regex() -> None:
    ctx = make_ctx(group_message("今天天气不错"))
    assert keyword("天气")(ctx) is True  # type: ignore[operator]
    assert keyword("下雨")(ctx) is False  # type: ignore[operator]
    assert regex("天气.错")(ctx) is True  # type: ignore[operator]
    assert ctx.regex_match is not None


def test_startswith_and_to_me() -> None:
    ctx = make_ctx(group_message("abc", at=10001))
    assert startswith("abc")(ctx) is True  # type: ignore[operator]
    assert to_me()(ctx) is True  # type: ignore[operator]

    plain = make_ctx(group_message("abc"))
    assert to_me()(plain) is False  # type: ignore[operator]


async def test_all_rules_and_not() -> None:
    ctx = make_ctx(group_message("/ping"))
    assert await all_rules(command("ping"), startswith("/"))(ctx) is True  # type: ignore[operator]
    assert await not_rule(keyword("ping"))(ctx) is False  # type: ignore[operator]
