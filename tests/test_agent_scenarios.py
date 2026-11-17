"""智能体场景回归：假模型驱动的群聊 Agent 跨模块端到端行为。

每个用例都从 OneBot 群消息入口（或 AgentRuntime 工具循环）开始，只用假连接、假模型和
临时插件，断言“谁被调用/没被调用、发送了什么、执行事实是什么状态、模型看到了什么”。
覆盖场景：

1. 工具授权：未授权工具不出现在模型 tools 中；同一轮执行期改配置后调用被拒绝。
2. 沙盒：自动参与时写文件/发文件/截屏被 tool_blocked 且无外部动作；明确 @ 后按配置放行。
3. stay_silent：只在自动参与且模型支持工具时提供；调用后不发消息、不落库假回复。
4. 工具结果：部分失败/超时/未发送分别留下 succeeded/uncertain/failed 执行事实，模型可读。
5. 重复调用提醒：同工具同参数第 3/5/8 次追加复用提醒，且提醒进入后续模型请求。
6. 追问与取消：任务中同发起者 @ 追问并入同一轮且不重做已执行工具；取消后不再执行后续工具。
7. 上下文：预算超限折叠后工具调用与结果配对完整，已执行动作不重放。
8. 引用与 @ 目标：引用正文、真实 @ 对象进入模型上下文，但不写进聊天历史。
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

from fllbot.llm.agent_runtime import AgentRuntime
from helpers import FakeConnection, group_message, make_bot

GROUP = 20002
SELF_ID = 10001
USER = 30003
SUPERUSER = 99999
LLM = {"enabled": True, "api_key": "fake", "context_window": 16000}


# --------------------------------------------------------------------------- #
# 场景辅助
# --------------------------------------------------------------------------- #


def _write_plugins(tmp_path: Path, sources: dict[str, str]) -> Path:
    directory = tmp_path / "plugins"
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        (directory / f"{name}.py").write_text(source, encoding="utf-8")
    return directory


async def _configure(
    bot, *, tools: list[str] | tuple[str, ...] = (), modes: tuple[str, ...] = ("all",), extra: dict | None = None
) -> None:
    policy = {
        "auto_group": True,
        "trigger_modes": list(modes),
        "cooldown_seconds": 0,
        "allowed_tools": list(tools),
    }
    policy.update(extra or {})
    await bot.permissions.save_group(GROUP, enabled=True, config={"policy": policy})


async def _settle(bot, timeout: float = 3.0) -> None:
    """等待事件队列处理完，并等控制类延迟回复（如取消确认）落定。"""
    await asyncio.wait_for(bot.dispatcher._queue.join(), timeout)
    pending = [task for task in bot.agent_tasks.acknowledgements if not task.done()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), timeout)


async def _deliver(bot, connection, payload: dict, timeout: float = 3.0) -> None:
    await bot.on_raw_event(connection, payload)
    await _settle(bot, timeout)


def _context() -> SimpleNamespace:
    return SimpleNamespace(data_current=True, self_id=11, tool_call_id="")


def _tool_names(options: dict) -> set[str]:
    return {item["function"]["name"] for item in options.get("tools") or []}


def _tool_results(messages: list[dict]) -> dict[str, dict]:
    return {
        message["tool_call_id"]: json.loads(message["content"])
        for message in messages
        if message.get("role") == "tool"
    }


async def _execution_rows(bot) -> dict[str, str]:
    rows = await (await bot.storage.db.execute("SELECT tool,status FROM tool_executions")).fetchall()
    return {row["tool"]: row["status"] for row in rows}


async def _chat(bot) -> list[tuple[str, str]]:
    rows = await bot.storage.recent_chat("group", GROUP, self_id=SELF_ID)
    return [(row.role, row.content) for row in rows]


async def _turn(bot):
    turns = await bot.storage.list_turns()
    assert len(turns["items"]) == 1, turns["items"]
    return await bot.storage.turn_detail(turns["items"][0]["turn_id"])


def _source(module: dict) -> list:
    return module["calls"]


# --------------------------------------------------------------------------- #
# 1) 工具授权过滤与执行期吊销
# --------------------------------------------------------------------------- #

_AUTH_PLUGIN = """
from fllbot.plugin import Plugin

plugin = Plugin('authdemo', enabled_by_default=True)
calls = []


@plugin.on_ai_tool('read', description='只读', parameters={'type': 'object', 'properties': {}})
async def read(ctx):
    calls.append('read')
    return {'read': True}


@plugin.on_ai_tool('write', description='写入', parameters={'type': 'object', 'properties': {}})
async def write(ctx):
    calls.append('write')
    return {'write': True}


@plugin.on_ai_tool('send', description='发送', parameters={'type': 'object', 'properties': {}})
async def send(ctx):
    calls.append('send')
    return {'sent': True}
"""


async def test_ungranted_tool_hidden_and_runtime_revocation_denied(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"authdemo": _AUTH_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    requests: list[tuple[list[dict], dict]] = []

    async def complete(messages, **options):
        requests.append((copy.deepcopy(messages), dict(options)))
        if len(requests) == 1:
            # 模型请求发出后才收回授权：执行期必须按最新配置拒绝。
            await _configure(bot, tools=["authdemo__read"])
            return SimpleNamespace(text="", tool_calls=[{
                "id": "revoked-1", "type": "function",
                "function": {"name": "authdemo__send", "arguments": "{}"},
            }])
        return SimpleNamespace(text="工具授权已失效，本次没有执行。", tool_calls=[])

    try:
        await _configure(bot, tools=["authdemo__send", "authdemo__read"])
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("帮我发一下文件", message_id=1))

        # 未授权工具从一开始就不在模型 tools 里。
        first_tools = _tool_names(requests[0][1])
        assert first_tools == {"agent__stay_silent", "authdemo__read", "authdemo__send"}
        assert "authdemo__write" not in first_tools

        module = bot.plugins.get("authdemo").ai_tools[0].callback.__globals__
        assert _source(module) == [], "被拒的工具回调不能执行"
        results = _tool_results(requests[1][0])
        assert results["revoked-1"]["error"] == "tool_permission_denied"
        # 拒绝发生在认领执行事实之前：没有工具执行记录，也没有外部动作。
        assert await _execution_rows(bot) == {}
        assert connection.texts() == ["工具授权已失效，本次没有执行。"]
        detail = await _turn(bot)
        assert detail["status"] == "delivered" and detail["tools"] == []
        assert any(item["phase"] == "tool" for item in detail["progress"])
    finally:
        await bot.stop()


# --------------------------------------------------------------------------- #
# 2) 沙盒裁决：自动参与全拒、明确 @ 按配置放行
# --------------------------------------------------------------------------- #

_SANDBOX_PLUGIN = """
from fllbot.plugin import Plugin

plugin = Plugin('sandboxdemo', enabled_by_default=True)
calls = []


@plugin.on_ai_tool(
    'write_file', description='写工作区文件',
    parameters={'type': 'object', 'properties': {'path': {'type': 'string'}},
                'required': ['path'], 'additionalProperties': False},
    effect='side_effecting', capabilities=('fs.write',),
)
async def write_file(ctx, path):
    calls.append(('write_file', path))
    return ctx.sandbox.files.write_text(path, 'sandbox payload')


@plugin.on_ai_tool(
    'send_attachment', description='发送附件',
    parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
    effect='side_effecting', capabilities=('qq.send_file',),
)
async def send_attachment(ctx):
    calls.append(('send_attachment',))
    await ctx.reply('ATTACHMENT_SENT')
    return {'sent': True}


@plugin.on_ai_tool(
    'capture_screen', description='截屏',
    parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
    effect='side_effecting', capabilities=('screen.capture',), risk='destructive',
)
async def capture_screen(ctx):
    calls.append(('capture_screen',))
    return {'captured': True}
"""


def _sandbox_batch(prefix: str) -> list[dict]:
    return [
        {"id": f"{prefix}-write", "type": "function",
         "function": {"name": "sandboxdemo__write_file", "arguments": '{"path": "notes/result.txt"}'}},
        {"id": f"{prefix}-send", "type": "function",
         "function": {"name": "sandboxdemo__send_attachment", "arguments": "{}"}},
        {"id": f"{prefix}-screen", "type": "function",
         "function": {"name": "sandboxdemo__capture_screen", "arguments": "{}"}},
    ]


async def test_sandbox_blocks_automatic_side_effects_and_allows_explicit_request(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"sandboxdemo": _SANDBOX_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    requests: list[tuple[list[dict], dict]] = []

    async def complete(messages, **options):
        requests.append((copy.deepcopy(messages), dict(options)))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=_sandbox_batch("auto"))
        if len(requests) == 2:
            return SimpleNamespace(text="这些操作需要你明确要求。", tool_calls=[])
        if len(requests) == 3:
            return SimpleNamespace(text="", tool_calls=_sandbox_batch("explicit"))
        return SimpleNamespace(text="已完成。", tool_calls=[])

    try:
        await _configure(
            bot,
            tools=["sandboxdemo__write_file", "sandboxdemo__send_attachment", "sandboxdemo__capture_screen"],
            modes=("all", "at"),
            extra={"sandbox_write": True, "sandbox_screen": True},
        )
        monkeypatch.setattr(bot.chat, "complete", complete)
        module = bot.plugins.get("sandboxdemo").ai_tools[0].callback.__globals__
        workspace = bot.sandbox.policy(GROUP).workspace_for("group", GROUP)

        # 自动参与（未 @）：三类写操作都在认领之前被沙盒拒绝，没有任何外部动作。
        auto_connection = FakeConnection()
        await _deliver(bot, auto_connection, group_message("帮我把资料写成文件再发送", message_id=1))
        assert _source(module) == []
        assert not (workspace / "notes" / "result.txt").exists()
        assert "ATTACHMENT_SENT" not in str(auto_connection.sent)
        assert _tool_names(requests[0][1]) >= {
            "agent__stay_silent", "sandboxdemo__write_file",
            "sandboxdemo__send_attachment", "sandboxdemo__capture_screen",
        }
        auto_results = _tool_results(requests[1][0])
        assert {value["error"] for value in auto_results.values()} == {"tool_blocked"}
        assert {value["reason"] for value in auto_results.values()} == {"automatic_side_effect"}
        assert any("不要重做" in str(item.get("content")) for item in requests[1][0]
                   if item.get("_fllbot_source", {}).get("kind") == "tool_failure")
        assert await _execution_rows(bot) == {}
        assert auto_connection.texts() == ["这些操作需要你明确要求。"]
        stats = bot.sandbox.stats
        assert stats["denied:automatic_side_effect"] == 3

        # 明确 @ 且群策略放开写入/截屏：同一个批次全部放行并留下成功执行事实。
        explicit_connection = FakeConnection()
        await _deliver(
            bot, explicit_connection,
            group_message("请把资料写成文件再发送截图", at=SELF_ID, user_id=SUPERUSER, message_id=2),
        )
        assert _source(module) == [
            ("write_file", "notes/result.txt"), ("send_attachment",), ("capture_screen",)
        ]
        assert (workspace / "notes" / "result.txt").read_text(encoding="utf-8") == "sandbox payload"
        assert "ATTACHMENT_SENT" in explicit_connection.texts()
        assert "已完成。" in explicit_connection.texts()
        assert await _execution_rows(bot) == {
            "sandboxdemo__write_file": "succeeded",
            "sandboxdemo__send_attachment": "succeeded",
            "sandboxdemo__capture_screen": "succeeded",
        }
        explicit_results = _tool_results(requests[3][0])
        assert explicit_results["explicit-write"]["path"] == "notes/result.txt"
        assert explicit_results["explicit-send"]["sent"] is True
        assert explicit_results["explicit-screen"]["captured"] is True
    finally:
        await bot.stop()


# --------------------------------------------------------------------------- #
# 3) stay_silent 的提供条件与静默收尾
# --------------------------------------------------------------------------- #


async def test_stay_silent_offered_only_for_automatic_participation(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM)
    connection = FakeConnection()
    requests: list[tuple[list[dict], dict]] = []
    external = bot.plugins.get("basic").ai_tools[0].qualified_name

    async def complete(messages, **options):
        requests.append((copy.deepcopy(messages), dict(options)))
        if len(requests) == 1:
            return SimpleNamespace(text="收到", tool_calls=[])
        return SimpleNamespace(text="", tool_calls=[{
            "id": "quiet-1", "type": "function",
            "function": {"name": "agent__stay_silent", "arguments": "{}"},
        }])

    try:
        await _configure(bot, tools=[external], modes=("all", "at"))
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("你好", at=SELF_ID, message_id=1))
        assert _tool_names(requests[0][1]) == {external}, "明确 @ 时不应提供 stay_silent"
        assert connection.texts() == ["收到"]

        silent_connection = FakeConnection()
        await _deliver(bot, silent_connection, group_message("今天先把资料整理一下吧", message_id=2))
        assert _tool_names(requests[1][1]) == {external, "agent__stay_silent"}, "自动参与应提供 stay_silent"

        # 静默收尾：不发送、不保存虚假 Bot 回复，但保留内部工具执行事实。
        assert silent_connection.sent == []
        turns = await bot.storage.list_turns()
        assert len(turns["items"]) == 2
        silent = turns["items"][0]
        assert (silent["status"], silent["reason"]) == ("silent", "explicit_silence")
        assert await bot.storage.turn_delivery_state(silent["turn_id"]) == []
        detail = await bot.storage.turn_detail(silent["turn_id"])
        assert [(item["tool"], item["status"]) for item in detail["tools"]] == [
            ("agent__stay_silent", "succeeded")
        ]
        assert await _chat(bot) == [("user", "你好"), ("assistant", "收到")]
    finally:
        await bot.stop()


async def test_stay_silent_hidden_when_model_cannot_call_tools(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm={**LLM, "supports_tools": False})
    connection = FakeConnection()
    requests: list[tuple[list[dict], dict]] = []
    external = bot.plugins.get("basic").ai_tools[0].qualified_name

    async def complete(messages, **options):
        requests.append((copy.deepcopy(messages), dict(options)))
        return SimpleNamespace(text="好", tool_calls=[])

    try:
        await _configure(bot, tools=[external], modes=("all",))
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("今天先把资料整理一下吧", message_id=1))
        assert _tool_names(requests[0][1]) == {external}, "模型不支持工具调用时不能提供 stay_silent"
        assert connection.texts() == ["好"]
    finally:
        await bot.stop()


# --------------------------------------------------------------------------- #
# 4) 工具结果：部分失败 / 超时 / 未发送的执行事实
# --------------------------------------------------------------------------- #

_RESULT_PLUGIN = """
from fllbot.plugin import Plugin

plugin = Plugin('resultdemo', enabled_by_default=True)
calls = []


@plugin.on_ai_tool('send_ok', description='成功的发送', parameters={'type': 'object', 'properties': {}},
                   effect='side_effecting')
async def send_ok(ctx):
    calls.append('send_ok')
    return {'sent': True}


@plugin.on_ai_tool('send_fail', description='失败的发送', parameters={'type': 'object', 'properties': {}},
                   effect='side_effecting')
async def send_fail(ctx):
    calls.append('send_fail')
    raise RuntimeError('external send failed')


@plugin.on_ai_tool('send_unsent', description='未发送的发送', parameters={'type': 'object', 'properties': {}},
                   effect='side_effecting')
async def send_unsent(ctx):
    calls.append('send_unsent')
    return {'sent': False, 'reason': 'driver refused'}
"""

_TIMEOUT_PLUGIN = """
import asyncio

from fllbot.plugin import Plugin

plugin = Plugin('timeoutdemo', enabled_by_default=True)
calls = []


@plugin.on_ai_tool('slow_send', description='会超时的发送',
                   parameters={'type': 'object', 'properties': {}},
                   effect='side_effecting', timeout=0.05)
async def slow_send(ctx):
    calls.append('slow_send')
    await asyncio.sleep(5)
    return {'sent': True}


@plugin.on_ai_tool('slow_read', description='会超时的读取',
                   parameters={'type': 'object', 'properties': {}},
                   effect='read_only', timeout=0.05)
async def slow_read(ctx):
    calls.append('slow_read')
    await asyncio.sleep(5)
    return {'read': True}
"""


async def test_partial_failure_and_unsent_results_are_facts_not_replayed(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"resultdemo": _RESULT_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    requests: list[tuple[list[dict], dict]] = []

    async def complete(messages, **options):
        requests.append((copy.deepcopy(messages), dict(options)))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=[
                {"id": "ok", "type": "function", "function": {"name": "resultdemo__send_ok", "arguments": "{}"}},
                {"id": "bad", "type": "function", "function": {"name": "resultdemo__send_fail", "arguments": "{}"}},
                {"id": "unsent", "type": "function",
                 "function": {"name": "resultdemo__send_unsent", "arguments": "{}"}},
            ])
        return SimpleNamespace(text="一张已发送，一张发送失败，另一张未发送。", tool_calls=[])

    try:
        await _configure(
            bot, tools=["resultdemo__send_ok", "resultdemo__send_fail", "resultdemo__send_unsent"],
            modes=("at",),
        )
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("发三张图", at=SELF_ID, message_id=1))
        module = bot.plugins.get("resultdemo").ai_tools[0].callback.__globals__
        assert _source(module) == ["send_ok", "send_fail", "send_unsent"], "每个动作只执行一次"
        assert len(requests) == 2, "结果已交给模型，不能重试已执行动作"
        assert await _execution_rows(bot) == {
            "resultdemo__send_ok": "succeeded",
            "resultdemo__send_fail": "uncertain",
            "resultdemo__send_unsent": "succeeded",
        }
        results = _tool_results(requests[1][0])
        assert results["ok"] == {"sent": True}
        assert results["bad"]["error"] == "tool_uncertain"
        assert results["unsent"] == {"sent": False, "reason": "driver refused"}
        reminders = [item for item in requests[1][0] if item.get("_fllbot_source", {}).get("kind") == "tool_failure"]
        assert len(reminders) == 1 and "不要重做" in reminders[0]["content"]
        assert connection.texts() == ["一张已发送，一张发送失败，另一张未发送。"]
        detail = await _turn(bot)
        assert detail["status"] == "delivered"
        assert [(item["call_id"], item["status"]) for item in detail["tools"]] == [
            ("ok", "succeeded"), ("bad", "uncertain"), ("unsent", "succeeded")
        ]
    finally:
        await bot.stop()


async def test_tool_timeout_facts_distinguish_uncertain_and_failed(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"timeoutdemo": _TIMEOUT_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    requests: list[tuple[list[dict], dict]] = []

    async def complete(messages, **options):
        requests.append((copy.deepcopy(messages), dict(options)))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=[
                {"id": "slow-send", "type": "function",
                 "function": {"name": "timeoutdemo__slow_send", "arguments": "{}"}},
                {"id": "slow-read", "type": "function",
                 "function": {"name": "timeoutdemo__slow_read", "arguments": "{}"}},
            ])
        return SimpleNamespace(text="两个操作都没有完成。", tool_calls=[])

    try:
        await _configure(bot, tools=["timeoutdemo__slow_send", "timeoutdemo__slow_read"], modes=("at",))
        monkeypatch.setattr(bot.chat, "complete", complete)
        await _deliver(bot, connection, group_message("发文件并读取资料", at=SELF_ID, message_id=1))
        module = bot.plugins.get("timeoutdemo").ai_tools[0].callback.__globals__
        assert _source(module) == ["slow_send", "slow_read"], "超时动作不能重放"
        assert await _execution_rows(bot) == {
            "timeoutdemo__slow_send": "uncertain",
            "timeoutdemo__slow_read": "failed",
        }
        results = _tool_results(requests[1][0])
        assert results["slow-send"]["error"] == "tool_uncertain"
        assert results["slow-read"]["error"] == "tool_failed"
        assert connection.texts() == ["两个操作都没有完成。"]
    finally:
        await bot.stop()


# --------------------------------------------------------------------------- #
# 5) 重复调用提醒：第 3/5/8 次追加并进入后续模型请求
# --------------------------------------------------------------------------- #


async def test_repeat_reminder_advises_at_third_fifth_and_eighth_call():
    entries = [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "GOAL"}]
    model_requests: list[list[dict]] = []
    executed: list[str] = []

    async def complete(messages, **options):
        model_requests.append(copy.deepcopy(messages))
        if len(model_requests) <= 8:
            return SimpleNamespace(text="", tool_calls=[{
                "id": str(len(model_requests)), "type": "function",
                "function": {"name": "lookup", "arguments": '{"n": 1}'},
            }])
        return SimpleNamespace(text="done", tool_calls=[])

    async def execute(ctx, name, arguments, registry):
        executed.append(arguments)
        return '{"ok": true}', False

    result = await AgentRuntime(complete).run(
        entries, scene="group", scene_id=123, ctx=_context(), execute=execute, agent=True
    )
    assert result.text == "done"
    assert len(executed) == 8 and set(executed) == {'{"n": 1}'}
    reminders = [item for item in entries if item.get("_fllbot_source", {}).get("kind") == "task_reminder"]
    assert len(reminders) == 3
    assert "连续 3 次" in reminders[0]["content"]
    assert "连续 5 次" in reminders[1]["content"]
    assert "连续 8 次" in reminders[2]["content"]
    assert all("不要重复已经完成或结果不确定的对外操作" in item["content"] for item in reminders)
    # 提醒确实在后续模型请求里可见：第 4/6/9 次请求分别多一条提醒。
    visible = [
        sum(1 for item in request if item.get("_fllbot_source", {}).get("kind") == "task_reminder")
        for request in model_requests
    ]
    assert visible == [0, 0, 0, 1, 1, 2, 2, 2, 3]


# --------------------------------------------------------------------------- #
# 6) 追问并入当前轮、取消后不执行后续工具
# --------------------------------------------------------------------------- #

_STEER_PLUGIN = """
import asyncio

from fllbot.plugin import Plugin

plugin = Plugin('steerdemo', enabled_by_default=True)
calls = []
entered = asyncio.Event()
release = asyncio.Event()


@plugin.on_ai_tool('do_step', description='执行一步', parameters={'type': 'object', 'properties': {}},
                   effect='side_effecting')
async def do_step(ctx):
    calls.append('do_step')
    entered.set()
    await release.wait()
    return {'done': True}
"""

_CANCEL_PLUGIN = """
import asyncio

from fllbot.plugin import Plugin

plugin = Plugin('cancelbatch', enabled_by_default=True)
calls = []
entered = asyncio.Event()
release = asyncio.Event()


@plugin.on_ai_tool('first_step', description='第一步', parameters={'type': 'object', 'properties': {}},
                   effect='side_effecting')
async def first_step(ctx):
    calls.append('first_step')
    entered.set()
    await release.wait()
    return {'done': True}


@plugin.on_ai_tool('second_step', description='第二步', parameters={'type': 'object', 'properties': {}},
                   effect='side_effecting')
async def second_step(ctx):
    calls.append('second_step')
    return {'done': True}
"""


async def test_mentioned_follow_up_joins_the_running_turn_without_replaying_tools(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"steerdemo": _STEER_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    requests: list[tuple[list[dict], dict]] = []

    async def complete(messages, **options):
        requests.append((copy.deepcopy(messages), dict(options)))
        if len(requests) == 1:
            return SimpleNamespace(text="", tool_calls=[{
                "id": "step-1", "type": "function",
                "function": {"name": "steerdemo__do_step", "arguments": "{}"},
            }])
        return SimpleNamespace(text="已按补充处理", tool_calls=[])

    try:
        await _configure(bot, tools=["steerdemo__do_step"], modes=("at",))
        monkeypatch.setattr(bot.chat, "complete", complete)
        module = bot.plugins.get("steerdemo").ai_tools[0].callback.__globals__

        await bot.on_raw_event(connection, group_message("整理资料", at=SELF_ID, message_id=1))
        await asyncio.wait_for(module["entered"].wait(), 2)
        await bot.on_raw_event(connection, group_message("只保留标题", at=SELF_ID, message_id=2))
        module["release"].set()
        await _settle(bot)

        assert len(requests) == 2
        assert requests[1][0][-1]["content"] == "只保留标题", "追问并入当前轮模型上下文"
        assert _source(module) == ["do_step"], "已执行的工具不因追问重做"
        assert await _execution_rows(bot) == {"steerdemo__do_step": "succeeded"}
        detail = await _turn(bot)
        assert detail["status"] == "delivered"
        assert [message["tool_call_id"] for message in requests[1][0] if message.get("role") == "tool"] == [
            "step-1"
        ]
        assert "已按补充处理" in connection.texts()
        assert any("还在处理" in text for text in connection.texts())
        assert await _chat(bot) == [
            ("user", "整理资料"),
            ("user", "只保留标题"),
            ("assistant", "已按补充处理"),
        ]
    finally:
        await bot.stop()


async def test_cancel_during_tool_batch_skips_remaining_tools(tmp_path, monkeypatch):
    plugin_dir = _write_plugins(tmp_path, {"cancelbatch": _CANCEL_PLUGIN})
    bot = await make_bot(tmp_path, plugin_dirs=[plugin_dir], llm=LLM)
    connection = FakeConnection()
    requests: list[tuple[list[dict], dict]] = []

    async def complete(messages, **options):
        requests.append((copy.deepcopy(messages), dict(options)))
        return SimpleNamespace(text="不应到达的回复", tool_calls=[
            {"id": "first", "type": "function",
             "function": {"name": "cancelbatch__first_step", "arguments": "{}"}},
            {"id": "second", "type": "function",
             "function": {"name": "cancelbatch__second_step", "arguments": "{}"}},
        ])

    try:
        await _configure(bot, tools=["cancelbatch__first_step", "cancelbatch__second_step"], modes=("at",))
        monkeypatch.setattr(bot.chat, "complete", complete)
        module = bot.plugins.get("cancelbatch").ai_tools[0].callback.__globals__

        await bot.on_raw_event(connection, group_message("执行两步", at=SELF_ID, message_id=1))
        await asyncio.wait_for(module["entered"].wait(), 2)
        await bot.on_raw_event(connection, group_message("/任务取消", message_id=2))
        await _settle(bot)
        module["release"].set()
        for task in list(bot.tool_executor._callbacks):
            await asyncio.gather(task, return_exceptions=True)

        assert _source(module) == ["first_step"], "取消后不能继续执行同批次后续工具"
        assert len(requests) == 1
        assert await _execution_rows(bot) == {"cancelbatch__first_step": "uncertain"}
        detail = await _turn(bot)
        assert (detail["status"], detail["reason"]) == ("cancelled", "user_cancelled")
        assert [item["tool"] for item in detail["tools"]] == ["cancelbatch__first_step"]
        assert any("已取消后续处理" in text for text in connection.texts())
        assert "不应到达的回复" not in connection.texts()
    finally:
        await bot.stop()


# --------------------------------------------------------------------------- #
# 7) 上下文折叠后工具调用与结果配对完整、不重放
# --------------------------------------------------------------------------- #


async def test_budget_compaction_preserves_tool_pairs_and_never_replays_actions():
    entries = [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "TASK GOAL"}]
    model_requests: list[list[dict]] = []
    executed: list[str] = []

    async def complete(messages, **options):
        model_requests.append(copy.deepcopy(messages))
        if options.get("task") == "summary":
            return SimpleNamespace(text="SUMMARY", tool_calls=[])
        if not any(message.get("role") == "tool" for message in messages):
            return SimpleNamespace(text="", tool_calls=[{
                "id": "long-1", "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }])
        return SimpleNamespace(text="done", tool_calls=[])

    async def execute(ctx, name, arguments, registry):
        executed.append(name)
        return json.dumps({"payload": "EVIDENCE" * 600}, ensure_ascii=False), False

    async def budget(options, messages, tools):
        return {"providers": [{"window": 2000, "output_reserve": 1000}]}

    result = await AgentRuntime(complete, budget).run(
        entries, scene="group", scene_id=123, ctx=_context(), execute=execute, agent=True
    )

    assert result.text == "done"
    assert executed == ["read"], "折叠后不能重放已执行的工具"
    assert len(model_requests) == 2
    second = model_requests[1]
    # 折叠保留了原始目标。
    assert any(item.get("_fllbot_task_input") and item.get("content") == "TASK GOAL" for item in second)
    # 每个工具调用仍有唯一配对结果，且结果被缩减而不是丢弃。
    assistant_indexes = [index for index, item in enumerate(second) if item.get("tool_calls")]
    assert len(assistant_indexes) == 1
    assistant = second[assistant_indexes[0]]
    call_ids = [call["id"] for call in assistant["tool_calls"]]
    paired = [item for item in second[assistant_indexes[0] + 1:] if item.get("role") == "tool"]
    assert [item["tool_call_id"] for item in paired] == call_ids == ["long-1"]
    assert paired[0]["content"].endswith("[任务上下文整理：结果已缩减；原操作已执行，不要重做]")
    assert not any(item.get("role") == "tool" and item["tool_call_id"] not in call_ids for item in second)


# --------------------------------------------------------------------------- #
# 8) 引用消息与真实 @ 目标进入模型上下文，不写进聊天历史
# --------------------------------------------------------------------------- #


async def test_quoted_message_and_mention_targets_reach_model_but_not_history(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm=LLM)
    connection = FakeConnection()
    connection.get_msg_results[777] = {
        "message_id": 777,
        "message_type": "group",
        "group_id": GROUP,
        "time": 1793720000,
        "sender": {"user_id": 40004, "nickname": "衣食无忧", "card": "", "role": "member"},
        "message": [
            {"type": "text", "data": {"text": "深渊怎么能没有点危险东西呢"}},
            {"type": "image", "data": {"url": "https://example.test/shot.png"}},
        ],
    }
    payload = group_message("", message=[
        {"type": "reply", "data": {"id": "777"}},
        {"type": "at", "data": {"qq": str(SELF_ID)}},
        {"type": "text", "data": {"text": "总结一下这条"}},
    ], message_id=1)
    sources: list[dict] = []
    complete_requests: list[tuple[list[dict], dict]] = []

    async def complete(messages, **options):
        complete_requests.append((copy.deepcopy(messages), dict(options)))
        return SimpleNamespace(text="总结如下", tool_calls=[])

    original_build = bot.chat.build_messages

    async def build_messages(*args, **kwargs):
        built = await original_build(*args, **kwargs)
        ctx = kwargs.get("ctx")
        snapshot = getattr(ctx, "context_snapshot", None)
        if snapshot is not None:
            sources.extend(snapshot.sources())
        return built

    try:
        await _configure(bot, modes=("all", "at"))
        monkeypatch.setattr(bot.chat, "complete", complete)
        monkeypatch.setattr(bot.chat, "build_messages", build_messages)
        await _deliver(bot, connection, payload)

        assert len(complete_requests) == 1
        last_content = complete_requests[0][0][-1]["content"]
        assert last_content.startswith("【被引用消息】")
        assert "衣食无忧(40004，成员)：深渊怎么能没有点危险东西呢" in last_content
        assert "【当前消息】" in last_content
        assert "[QQ @ 目标：机器人自身]" in last_content
        assert "测试用户(30003，成员)：[QQ @ 目标：机器人自身]\n总结一下这条" in last_content
        system = complete_requests[0][0][0]["content"]
        assert "【被引用消息】" in system and "[QQ @ 目标]" in system and "本轮任务边界" in system
        assert [action for action, _ in connection.calls].count("get_msg") == 1
        assert ("get_msg", {"message_id": 777}) in connection.calls

        # 来源元数据记录引用对象，但聊天历史只保存用户原话。
        current = next(item for item in sources if item.get("kind") == "current")
        assert current["quote"] == {"message_id": 777, "user_id": 40004, "error": ""}
        assert await _chat(bot) == [("user", "总结一下这条"), ("assistant", "总结如下")]
        detail = await _turn(bot)
        stored = await (await bot.storage.db.execute(
            "SELECT content FROM turn_messages WHERE turn_id=?", (detail["turn_id"],)
        )).fetchone()
        assert stored["content"] == "总结一下这条"
        assert detail["context_metadata"]["request_version"]
        assert detail["context_metadata"]["source_counts"] == {"system": 1, "current": 1}
    finally:
        await bot.stop()
