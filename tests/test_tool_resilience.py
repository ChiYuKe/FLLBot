"""工具执行层韧性：参数自愈、失败信息自然化与同轮重复只读调用上限。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import ValidationError

from fllbot.onebot.models import parse_event
from fllbot.plugin.base import AIToolSpec
from fllbot.plugin.context import Context
from fllbot.tool_executor import parse_arguments, repair_arguments
from fllbot.tracing import MessageTrace, activate
from helpers import FakeConnection, group_message, make_bot

GROUP = 20002


def _spec(parameters: dict, *, name: str = "probe", effect: str = "read_only",
          capabilities=(), callback=None) -> AIToolSpec:
    return AIToolSpec(
        name,
        "测试工具",
        parameters,
        callback or AsyncMock(return_value={"ok": True}),
        plugin_name="example",
        effect=effect,
        capabilities=tuple(capabilities),
    )


def _ctx(bot, *, turn_id: str = "turn-resilience", call_id: str = "call") -> Context:
    ctx = Context(bot, FakeConnection(), parse_event(group_message("hello")))
    ctx.turn_id = turn_id
    ctx.tool_call_id = call_id
    return ctx


async def _bot(tmp_path):
    bot = await make_bot(tmp_path, builtin=False)
    await bot.permissions.set_plugin_enabled("example", True)
    return bot


# --------------------------------------------------------------------------- #
# 参数自愈：只做无损类型修正，修正后仍严格校验
# --------------------------------------------------------------------------- #

_REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "count": {"type": "integer", "minimum": 0, "description": "要查询的数量"},
        "ratio": {"type": "number"},
        "enabled": {"type": "boolean"},
        "label": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "rows": {"type": "array", "items": {"type": "integer"}},
        "meta": {"type": "object", "properties": {"score": {"type": "integer"}, "name": {"type": "string"}}},
    },
    "required": ["count"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"count": "42"}, {"count": 42}),
        ({"count": " 7 "}, {"count": 7}),
        ({"count": 5.0}, {"count": 5}),
        ({"count": 1, "ratio": "2.5"}, {"count": 1, "ratio": 2.5}),
        ({"count": 1, "ratio": "3"}, {"count": 1, "ratio": 3}),
        ({"count": 1, "enabled": "true"}, {"count": 1, "enabled": True}),
        ({"count": 1, "enabled": "False"}, {"count": 1, "enabled": False}),
        ({"count": 1, "label": 42}, {"count": 1, "label": "42"}),
        ({"count": 1, "label": True}, {"count": 1, "label": "true"}),
        ({"count": 1, "tags": "solo"}, {"count": 1, "tags": ["solo"]}),
        ({"count": 1, "rows": ["1", 2]}, {"count": 1, "rows": [1, 2]}),
        ({"count": 1, "meta": '{"score": "9", "name": 3}'}, {"count": 1, "meta": {"score": 9, "name": "3"}}),
    ],
)
def test_lossless_argument_repair_passes_strict_validation(raw, expected):
    spec = _spec(_REPAIR_SCHEMA)
    assert parse_arguments(raw, spec, repair=True) == expected


@pytest.mark.parametrize(
    ("raw", "constraint"),
    [
        ({"count": "abc"}, "type"),
        ({"count": 1, "ratio": "not-a-number"}, "type"),
        ({"count": 1, "enabled": "yes"}, "type"),
        ({"count": -1}, "minimum"),
        ({"count": 1, "extra": 2}, "additionalProperties"),
        ({}, "required"),
        ({"count": 1}, "required"),
        ({"count": 1, "label": "x", "tags": [None]}, "type"),
        ({"count": 1, "meta": "not-json"}, "type"),
        ({"count": 1, "meta": '[1, 2]'}, "type"),
    ],
)
def test_unrepairable_arguments_still_fail_strictly(raw, constraint):
    raw = dict(raw)
    if constraint == "required" and "count" in raw:
        raw.pop("count")
    spec = _spec(
        {**_REPAIR_SCHEMA, "properties": {**_REPAIR_SCHEMA["properties"],
                                         "mode": {"type": "string", "enum": ["a", "b"]}}}
    )
    with pytest.raises(ValidationError) as info:
        parse_arguments(raw, spec, repair=True)
    assert info.value.validator == constraint


def test_partial_repair_is_revalidated_and_reports_remaining_error():
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}, "mode": {"type": "string", "enum": ["a", "b"]}},
        "required": ["count", "mode"],
        "additionalProperties": False,
    }
    with pytest.raises(ValidationError) as info:
        parse_arguments({"count": "5", "mode": "c"}, _spec(schema), repair=True)
    assert info.value.validator == "enum"
    assert list(info.value.absolute_path) == ["mode"]


async def test_repair_is_used_for_execution_and_traced_without_values(tmp_path):
    bot = await _bot(tmp_path)
    callback = AsyncMock(return_value={"total": 6})
    spec = _spec(_REPAIR_SCHEMA, callback=callback)
    ctx = _ctx(bot, call_id="repaired-call")
    trace = MessageTrace(ctx.event, ctx.connection)
    try:
        with activate(trace):
            output, silent = await bot.tool_executor.execute(
                ctx, spec.qualified_name, '{"count": "6", "tags": 7}', {spec.qualified_name: (SimpleNamespace(name="example"), spec)}
            )
        assert json.loads(output) == {"total": 6}
        assert not silent
        callback.assert_awaited_once()
        assert callback.await_args.kwargs == {"count": 6, "tags": ["7"]}
        steps = trace.summary()["steps"]
        assert any(step["stage"] == "tool" and step["code"] == "arguments_repaired"
                   and step["status"] == "passed" for step in steps)
        assert "6" not in {step["component"] for step in steps}
        assert "PRIVATE" not in json.dumps(steps)
    finally:
        await bot.stop()


# --------------------------------------------------------------------------- #
# 失败 payload：统一中文 message、字段路径、约束摘要、不泄露原值
# --------------------------------------------------------------------------- #


async def test_invalid_arguments_details_are_actionable_and_private(tmp_path):
    bot = await _bot(tmp_path)
    callback = AsyncMock()
    spec = _spec(_REPAIR_SCHEMA, callback=callback)
    ctx = _ctx(bot, call_id="invalid-call")
    try:
        output, silent = await bot.tool_executor.execute(
            ctx, spec.qualified_name, '{"count": "PRIVATE-ARGUMENT", "extra": 1}',
            {spec.qualified_name: (SimpleNamespace(name="example"), spec)},
        )
        payload = json.loads(output)
        assert payload["error"] == "invalid_arguments"
        assert isinstance(payload["message"], str) and payload["message"].strip()
        details = payload["details"]
        assert details["field"] == "count"
        assert details["constraint"] == "type"
        assert details["expected_summary"]
        assert details["description"] == "要查询的数量"
        assert details["hint"] and "count" in details["hint"]
        assert "PRIVATE-ARGUMENT" not in output
        callback.assert_not_awaited()
        assert not silent
    finally:
        await bot.stop()


async def test_nested_field_path_is_human_readable(tmp_path):
    bot = await _bot(tmp_path)
    spec = _spec(
        {
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
            },
            "additionalProperties": False,
        }
    )
    ctx = _ctx(bot, call_id="nested-call")
    try:
        output, _ = await bot.tool_executor.execute(
            ctx, spec.qualified_name, '{"rows": [[1, 2], [3, "PRIVATE-NESTED"]]}',
            {spec.qualified_name: (SimpleNamespace(name="example"), spec)},
        )
        payload = json.loads(output)
        assert payload["details"]["field"] == "rows[1][1]"
        assert "PRIVATE-NESTED" not in output
    finally:
        await bot.stop()


async def test_json_decode_failure_keeps_hint_and_message(tmp_path):
    bot = await _bot(tmp_path)
    ctx = _ctx(bot, call_id="json-call")
    spec = _spec({"type": "object", "properties": {}})
    try:
        output, _ = await bot.tool_executor.execute(
            ctx, spec.qualified_name, '{"count": 1', {spec.qualified_name: (SimpleNamespace(name="example"), spec)}
        )
        payload = json.loads(output)
        assert payload["error"] == "invalid_arguments"
        assert payload["details"]["constraint"] == "json"
        assert payload["details"]["hint"]
        assert payload["message"]
    finally:
        await bot.stop()


async def test_unavailable_and_permission_failures_have_chinese_messages(tmp_path):
    bot = await _bot(tmp_path)
    ctx = _ctx(bot, call_id="missing-call")
    try:
        payload = json.loads((await bot.tool_executor.execute(ctx, "example__missing", "{}", {}))[0])
        assert payload["error"] == "tool_unavailable"
        assert payload["message"]

        spec = _spec({"type": "object", "properties": {}})
        await bot.permissions.set_plugin_enabled("example", False)
        payload = json.loads((await bot.tool_executor.execute(
            ctx, spec.qualified_name, "{}", {spec.qualified_name: (SimpleNamespace(name="example"), spec)}
        ))[0])
        assert payload["error"] == "tool_permission_denied"
        assert payload["message"]
    finally:
        await bot.stop()


async def test_uncertain_and_failed_failures_explain_retry_policy(tmp_path):
    bot = await _bot(tmp_path)
    ctx = _ctx(bot, call_id="timeout-call")

    async def hang(tool_ctx):
        await asyncio.Event().wait()

    side_spec = _spec({"type": "object", "properties": {}}, effect="side_effecting", callback=hang)
    side_spec.timeout = 0.01
    read_spec = _spec({"type": "object", "properties": {}}, effect="read_only", callback=hang)
    read_spec.timeout = 0.01
    try:
        payload = json.loads((await bot.tool_executor.execute(
            ctx, side_spec.qualified_name, "{}", {side_spec.qualified_name: (SimpleNamespace(name="example"), side_spec)}
        ))[0])
        assert payload["error"] == "tool_uncertain"
        assert "不确定" in payload["message"] and "不要自动重试" in payload["message"]

        ctx.tool_call_id = "read-timeout"
        payload = json.loads((await bot.tool_executor.execute(
            ctx, read_spec.qualified_name, "{}", {read_spec.qualified_name: (SimpleNamespace(name="example"), read_spec)}
        ))[0])
        assert payload["error"] == "tool_failed"
        assert payload["message"]
    finally:
        await bot.stop()


async def test_busy_and_stopping_failures_have_chinese_messages(tmp_path):
    bot = await _bot(tmp_path)
    ctx = _ctx(bot)
    spec = _spec({"type": "object", "properties": {}})
    registry = {spec.qualified_name: (SimpleNamespace(name="example"), spec)}
    try:
        bot.tool_executor.max_callbacks = 0
        payload = json.loads((await bot.tool_executor.execute(ctx, spec.qualified_name, "{}", registry))[0])
        assert payload["error"] == "tool_executor_busy" and payload["message"]

        bot.tool_executor.max_callbacks = 32
        bot.tool_executor.closing = True
        payload = json.loads((await bot.tool_executor.execute(ctx, spec.qualified_name, "{}", registry))[0])
        assert payload["error"] == "service_stopping" and payload["message"]
    finally:
        bot.tool_executor.closing = False
        await bot.stop()


async def test_sandbox_blocked_payload_keeps_reason_and_adds_message(tmp_path):
    bot = await _bot(tmp_path)
    callback = AsyncMock()
    spec = _spec(
        {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False},
        effect="side_effecting",
        capabilities=("fs.write",),
        callback=callback,
    )
    ctx = _ctx(bot, call_id="sandbox-call")
    try:
        output, silent = await bot.tool_executor.execute(
            ctx, spec.qualified_name, '{"path": "PRIVATE-PATH.txt"}',
            {spec.qualified_name: (SimpleNamespace(name="example"), spec)},
        )
        payload = json.loads(output)
        assert payload["error"] == "tool_blocked"
        assert payload["reason"] == "write_disabled"
        assert payload["hint"] and payload["message"]
        assert "PRIVATE-PATH" not in output
        callback.assert_not_awaited()
        assert not silent
    finally:
        await bot.stop()


# --------------------------------------------------------------------------- #
# 同轮相同工具 + 相同参数的只读调用硬上限
# --------------------------------------------------------------------------- #


async def test_read_only_repeat_call_is_blocked_after_limit(tmp_path):
    bot = await _bot(tmp_path)
    callback = AsyncMock(return_value={"ok": True})
    spec = _spec({"type": "object", "properties": {}}, callback=callback)
    registry = {spec.qualified_name: (SimpleNamespace(name="example"), spec)}
    try:
        bot.tool_executor.repeat_limit = 3
        results = []
        for index in range(4):
            ctx = _ctx(bot, call_id=f"repeat-{index}")
            results.append(await bot.tool_executor.execute(ctx, spec.qualified_name, "{}", registry))
        assert [json.loads(output).get("error") for output, _ in results[:3]] == [None, None, None]
        payload = json.loads(results[3][0])
        assert payload["error"] == "tool_repeat_limit"
        assert "复用已有结果" in payload["message"]
        assert callback.await_count == 3
        rows = await (await bot.storage.db.execute("SELECT count(*) FROM tool_executions")).fetchone()
        assert rows[0] == 3, "被上限拦下的调用不能写执行事实"
        assert bot.tool_executor.status()["repeat_blocked"] == 1
    finally:
        await bot.stop()


async def test_repeat_limit_defaults_to_twelve_and_resets_per_turn(tmp_path):
    bot = await _bot(tmp_path)
    callback = AsyncMock(return_value={"ok": True})
    spec = _spec({"type": "object", "properties": {}}, callback=callback)
    registry = {spec.qualified_name: (SimpleNamespace(name="example"), spec)}
    try:
        assert bot.tool_executor.repeat_limit == 12
        for index in range(12):
            ctx = _ctx(bot, turn_id="turn-a", call_id=f"a-{index}")
            output, _ = await bot.tool_executor.execute(ctx, spec.qualified_name, "{}", registry)
            assert "error" not in json.loads(output)
        ctx = _ctx(bot, turn_id="turn-a", call_id="a-12")
        payload = json.loads((await bot.tool_executor.execute(ctx, spec.qualified_name, "{}", registry))[0])
        assert payload["error"] == "tool_repeat_limit"
        # 换一轮后计数重新开始。
        ctx = _ctx(bot, turn_id="turn-b", call_id="b-0")
        output, _ = await bot.tool_executor.execute(ctx, spec.qualified_name, "{}", registry)
        assert "error" not in json.loads(output)
        assert callback.await_count == 13
    finally:
        await bot.stop()


async def test_different_arguments_and_side_effecting_tools_are_unaffected(tmp_path):
    bot = await _bot(tmp_path)
    read_callback = AsyncMock(return_value={"ok": True})
    read_spec = _spec({"type": "object", "properties": {"n": {"type": "integer"}}},
                      name="read", callback=read_callback)
    write_callback = AsyncMock(return_value={"sent": True})
    write_spec = _spec(
        {"type": "object", "properties": {"n": {"type": "integer"}}},
        name="write",
        effect="side_effecting",
        callback=write_callback,
    )
    registry = {
        read_spec.qualified_name: (SimpleNamespace(name="example"), read_spec),
        write_spec.qualified_name: (SimpleNamespace(name="example"), write_spec),
    }
    try:
        bot.tool_executor.repeat_limit = 2
        for index in range(5):
            ctx = _ctx(bot, call_id=f"read-{index}")
            output, _ = await bot.tool_executor.execute(
                ctx, read_spec.qualified_name, json.dumps({"n": index}), registry)
            assert "error" not in json.loads(output)
        assert read_callback.await_count == 5, "不同参数各自计数，不能互相影响"
        for index in range(5):
            ctx = _ctx(bot, call_id=f"write-{index}")
            output, _ = await bot.tool_executor.execute(
                ctx, write_spec.qualified_name, "{}", registry)
            assert "error" not in json.loads(output)
        assert write_callback.await_count == 5, "写入/发送类工具不受重复只读上限约束"
    finally:
        await bot.stop()


async def test_repeat_counter_memory_is_bounded(tmp_path):
    bot = await _bot(tmp_path)
    spec = _spec({"type": "object", "properties": {"n": {"type": "integer"}}})
    registry = {spec.qualified_name: (SimpleNamespace(name="example"), spec)}
    try:
        bot.tool_executor.max_repeat_keys = 4
        for index in range(10):
            ctx = _ctx(bot, call_id=f"bounded-{index}")
            await bot.tool_executor.execute(ctx, spec.qualified_name, json.dumps({"n": index}), registry)
        assert bot.tool_executor.status()["repeat_keys"] <= 4
    finally:
        await bot.stop()


def test_repair_helper_returns_none_when_nothing_can_be_fixed():
    spec = _spec({"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]})
    assert repair_arguments({"count": "abc"}, spec) is None
