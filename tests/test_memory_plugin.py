"""memory 插件：长期记忆工具、命令、场景隔离与自动参与边界，全程离线。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from fllbot.onebot.models import parse_event
from fllbot.plugin.agent import instruction_sections
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot, private_message

ROOT = Path(__file__).resolve().parents[1]
GROUP = 20002
OTHER_GROUP = 20003
SELF = 10001
OTHER_SELF = 10002
USER = 30003
OTHER = 30004
DAY = 86_400


async def setup_memory(tmp_path, **settings):
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    shutil.copytree(ROOT / "plugins" / "memory", plugin_root / "memory")
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[plugin_root])
    await bot.permissions.save_group(GROUP, enabled=True, config={})
    memory = bot.plugins.get("memory")
    assert memory is not None
    if settings:
        await memory.settings.update(settings)
    return bot, memory


def tool(plugin, name):
    return next(spec for spec in plugin.ai_tools if spec.name == name).callback


def context(bot, plugin, text="", *, user_id=USER, group_id=GROUP, self_id=SELF, at=None, private=False):
    event = (
        private_message(text, self_id=self_id, user_id=user_id)
        if private
        else group_message(text, self_id=self_id, group_id=group_id, user_id=user_id, at=at)
    )
    ctx = Context(bot, FakeConnection(self_id), parse_event(event))
    ctx.plugin = plugin
    return ctx


def registry_of(bot):
    entries = bot.tools.snapshot().entries
    return {name: (entry.owner, entry.spec) for name, entry in entries.items()}


async def execute(bot, ctx, name, arguments):
    registry = registry_of(bot)
    output, silent = await bot.tool_executor.execute(
        ctx, name, arguments, registry, refresh=lambda _: ([], registry)
    )
    return json.loads(output), silent


def fact_rows(memory):
    connection = sqlite3.connect(memory.data_dir / "memory.sqlite3")
    try:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM facts ORDER BY id").fetchall()
    finally:
        connection.close()


def age_rows(memory, seconds):
    connection = sqlite3.connect(memory.data_dir / "memory.sqlite3")
    try:
        connection.execute(
            "UPDATE facts SET created_at=created_at-?, expires_at=expires_at-?", (seconds, seconds)
        )
        connection.commit()
    finally:
        connection.close()


async def dispatch(bot, text, *, user_id=USER, group_id=GROUP, private=False, message_id=1):
    event = (
        private_message(text, user_id=user_id, message_id=message_id)
        if private
        else group_message(text, user_id=user_id, group_id=group_id, message_id=message_id)
    )
    connection = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(event), connection)
    return connection


async def test_package_declares_tools_settings_loop_and_data_dir(tmp_path):
    bot, memory = await setup_memory(tmp_path)
    try:
        specs = {spec.name: spec for spec in memory.ai_tools}
        assert set(specs) == {"remember", "recall", "forget"}
        assert specs["remember"].effect == "side_effecting"
        assert specs["remember"].risk == "write"
        assert specs["remember"].capabilities == ("memory.write",)
        assert specs["forget"].effect == "side_effecting"
        assert specs["forget"].risk == "write"
        assert specs["forget"].capabilities == ("memory.write",)
        assert specs["recall"].effect == "read_only"
        assert specs["recall"].risk == "read"
        assert specs["recall"].capabilities == ("memory.read",)
        for spec in specs.values():
            assert spec.parameters["type"] == "object"
            assert spec.parameters["additionalProperties"] is False
            assert spec.qualified_name.startswith("memory__")
        # 写工具不能声明用户 id 之类可越权的参数。
        assert "user_id" not in specs["remember"].parameters["properties"]
        assert "user_id" not in specs["recall"].parameters["properties"]
        keys = {item["key"] for item in memory.settings.schema()}
        assert {"enabled", "max_facts", "max_content_chars", "retention_days", "auto_hint"} <= keys
        loops = {loop.name: loop for loop in memory.loops}
        assert loops["prune_expired"].seconds == 3600
        assert memory.data_dir == tmp_path / "data/plugins/memory"
        assert [item.name for item in memory.agent_instructions] == [
            "memory_remember",
            "memory_recall",
            "memory_forget",
        ]
    finally:
        await bot.stop()


async def test_remember_recall_forget_roundtrip_and_duplicate(tmp_path):
    bot, memory = await setup_memory(tmp_path)
    try:
        remember, recall, forget = (
            tool(memory, "remember"),
            tool(memory, "recall"),
            tool(memory, "forget"),
        )
        ctx = context(bot, memory, "记住我喜欢猫")
        created = await remember(ctx, "我喜欢猫", kind="preference", tags="宠物, 猫")
        assert created["id"] > 0
        assert created["kind"] == "preference"
        assert created["tags"] == ["宠物", "猫"]
        fact_id = created["id"]

        found = await recall(ctx, query="猫")
        assert found["count"] == 1
        assert found["items"][0] == {
            "id": fact_id,
            "content": "我喜欢猫",
            "kind": "preference",
            "tags": ["宠物", "猫"],
            "created_at": created["created_at"],
        }
        # 内容或标签命中都能回查。
        assert (await recall(ctx, query="宠物"))["items"][0]["id"] == fact_id
        assert (await recall(ctx, query="不存在"))["items"] == []

        again = await remember(ctx, "我喜欢猫")
        assert again["id"] == fact_id and again["duplicate"] is True
        assert len(fact_rows(memory)) == 1

        other = context(bot, memory, user_id=OTHER)
        assert (await recall(other))["items"] == []
        assert (await forget(other, fact_id=fact_id))["deleted"] == 0
        assert (await recall(ctx))["items"][0]["id"] == fact_id

        assert (await forget(ctx))["error"] == "need_target"
        assert (await forget(ctx, fact_id=fact_id, all=True))["error"] == "conflicting_target"
        assert (await forget(ctx, fact_id=fact_id))["deleted"] == 1
        assert (await recall(ctx))["items"] == []
        assert (await forget(ctx, fact_id=fact_id))["error"] == "not_found"

        await remember(ctx, "事实一")
        await remember(ctx, "事实二")
        cleared = await forget(ctx, all=True)
        assert cleared == {"deleted": 2, "all": True}
        assert fact_rows(memory) == []
    finally:
        await bot.stop()


async def test_scene_user_and_account_isolation(tmp_path):
    bot, memory = await setup_memory(tmp_path)
    try:
        remember, recall, forget = (
            tool(memory, "remember"),
            tool(memory, "recall"),
            tool(memory, "forget"),
        )
        group_ctx = context(bot, memory)
        private_ctx = context(bot, memory, private=True)
        other_group = context(bot, memory, group_id=OTHER_GROUP)
        other_user = context(bot, memory, user_id=OTHER)
        other_bot = context(bot, memory, self_id=OTHER_SELF)

        group_fact = await remember(group_ctx, "在群里说过的偏好")
        assert (await recall(group_ctx))["items"][0]["id"] == group_fact["id"]
        assert (await recall(private_ctx))["items"] == []
        assert (await recall(other_group))["items"] == []
        assert (await recall(other_user))["items"] == []
        assert (await recall(other_bot))["items"] == []

        private_fact = await remember(private_ctx, "私聊里告诉我的事")
        assert (await recall(private_ctx))["items"][0]["id"] == private_fact["id"]
        assert [item["id"] for item in (await recall(group_ctx))["items"]] == [group_fact["id"]]

        # 跨场景/跨账号的编号都删不掉。
        assert (await forget(private_ctx, fact_id=group_fact["id"]))["deleted"] == 0
        assert (await forget(other_group, fact_id=group_fact["id"]))["deleted"] == 0
        assert (await forget(other_bot, fact_id=group_fact["id"]))["deleted"] == 0
        assert (await recall(group_ctx))["items"][0]["id"] == group_fact["id"]

        # 同一用户在不同机器人账号下的记录也隔离。
        bot_fact = await remember(other_bot, "另一个账号的记忆")
        assert (await recall(other_bot))["items"][0]["id"] == bot_fact["id"]
        assert (await recall(group_ctx))["items"][0]["id"] == group_fact["id"]
    finally:
        await bot.stop()


async def test_limits_dedupe_and_input_validation(tmp_path):
    bot, memory = await setup_memory(tmp_path, max_facts=3, max_content_chars=30)
    try:
        remember, recall = tool(memory, "remember"), tool(memory, "recall")
        ctx = context(bot, memory)

        tagged = await remember(ctx, "我喜欢喝咖啡", kind="preference", tags="饮料,咖啡,饮料")
        assert tagged["tags"] == ["饮料", "咖啡"]
        assert (await recall(ctx, query="咖啡"))["items"][0]["id"] == tagged["id"]

        assert (await remember(ctx, "第二条事实"))["id"]
        assert (await remember(ctx, "第三条事实"))["id"]
        overflow = await remember(ctx, "第四条事实")
        assert overflow["error"] == "limit_reached"
        assert overflow["max_facts"] == 3
        assert len(fact_rows(memory)) == 3
        # 重复内容不占新额度。
        repeated = await remember(ctx, "我喜欢喝咖啡")
        assert repeated["id"] == tagged["id"] and repeated["duplicate"] is True
        assert len(fact_rows(memory)) == 3

        assert (await remember(ctx, "   "))["error"] == "empty_content"
        too_long = await remember(ctx, "长" * 40)
        assert too_long["error"] == "content_too_long"
        assert "30" in too_long["message"]
        secret = await remember(ctx, "我的密码是 hunter2")
        assert secret["error"] == "sensitive_content"
        assert (await remember(ctx, "测试", kind="unknown"))["error"] == "invalid_kind"
        assert (await recall(ctx, query="x" * 101))["error"] == "query_too_long"
        assert len(fact_rows(memory)) == 3
    finally:
        await bot.stop()


async def test_retention_cleanup_on_write_and_periodic_loop(tmp_path):
    bot, memory = await setup_memory(tmp_path, retention_days=1)
    try:
        remember, recall = tool(memory, "remember"), tool(memory, "recall")
        ctx = context(bot, memory)

        first = await remember(ctx, "会过期的记忆")
        age_rows(memory, 3 * DAY)
        # 读取按 expires_at 过滤；尚未删除，但不可见。
        assert (await recall(ctx))["items"] == []
        assert [row["id"] for row in fact_rows(memory)] == [first["id"]]

        second = await remember(ctx, "写入时清理过期记录")
        remaining = [row["id"] for row in fact_rows(memory)]
        assert first["id"] not in remaining
        assert second["id"] in remaining

        third = await remember(ctx, "交给周期任务清理")
        age_rows(memory, 3 * DAY)
        prune = next(loop for loop in memory.loops if loop.name == "prune_expired")
        await prune.callback(bot)
        assert third["id"] not in [row["id"] for row in fact_rows(memory)]
        assert fact_rows(memory) == []
    finally:
        await bot.stop()


async def test_commands_list_and_forget_only_own_records(tmp_path):
    bot, memory = await setup_memory(tmp_path)
    try:
        remember = tool(memory, "remember")
        ctx = context(bot, memory)

        empty = await dispatch(bot, "/记忆", message_id=1)
        assert "还没有长期记忆" in empty.last_text()

        first = await remember(ctx, "我喜欢猫")
        second = await remember(ctx, "我周末去爬山", kind="event", tags="运动")
        listed = await dispatch(bot, "/记忆", message_id=2)
        text = listed.last_text()
        assert f"#{first['id']}" in text and "我喜欢猫" in text
        assert f"#{second['id']}" in text and "我周末去爬山" in text
        assert "长期记忆" in text

        # 别人看不到这段记忆，也删不掉本人的编号。
        other_list = await dispatch(bot, "/记忆", user_id=OTHER, message_id=3)
        assert "还没有长期记忆" in other_list.last_text()
        other_forget = await dispatch(bot, f"/忘记 {first['id']}", user_id=OTHER, message_id=4)
        assert "没找到" in other_forget.last_text()
        assert (await tool(memory, "recall")(ctx))["count"] == 2

        removed = await dispatch(bot, f"/忘记 {first['id']}", message_id=5)
        assert "已忘记" in removed.last_text()
        assert [item["id"] for item in (await tool(memory, "recall")(ctx))["items"]] == [second["id"]]

        invalid = await dispatch(bot, "/忘记", message_id=6)
        assert "用法" in invalid.last_text()
        invalid = await dispatch(bot, "/忘记 不是数字", message_id=7)
        assert "用法" in invalid.last_text()

        cleared = await dispatch(bot, "/忘记 全部", message_id=8)
        assert "1 条" in cleared.last_text()
        assert fact_rows(memory) == []
        again = await dispatch(bot, "/忘记 全部", message_id=9)
        assert "还没有长期记忆" in again.last_text()
    finally:
        await bot.stop()


async def test_commands_keep_private_and_group_memories_separate(tmp_path):
    bot, memory = await setup_memory(tmp_path)
    try:
        remember = tool(memory, "remember")
        group_ctx = context(bot, memory)
        private_ctx = context(bot, memory, private=True)

        group_fact = await remember(group_ctx, "群里的记忆")
        private_fact = await remember(private_ctx, "私聊的记忆")

        group_list = await dispatch(bot, "/记忆", message_id=1)
        assert "群里的记忆" in group_list.last_text()
        assert "私聊的记忆" not in group_list.last_text()

        private_list = await dispatch(bot, "/记忆", private=True, message_id=2)
        assert "私聊的记忆" in private_list.last_text()
        assert "群里的记忆" not in private_list.last_text()

        # 私聊里的 /忘记 不能删群记忆。
        cross = await dispatch(bot, f"/忘记 {group_fact['id']}", private=True, message_id=3)
        assert "没找到" in cross.last_text()
        assert (await tool(memory, "recall")(group_ctx))["items"][0]["id"] == group_fact["id"]

        private_forget = await dispatch(bot, "/忘记 全部", private=True, message_id=4)
        assert "1 条" in private_forget.last_text()
        assert (await tool(memory, "recall")(private_ctx))["items"] == []
        assert (await tool(memory, "recall")(group_ctx))["items"][0]["id"] == group_fact["id"]
        assert private_fact["id"] != group_fact["id"]
    finally:
        await bot.stop()


async def test_automatic_participation_rejects_writes_but_allows_recall(tmp_path):
    bot, memory = await setup_memory(tmp_path)
    try:
        explicit = context(bot, memory, "记住我的偏好")
        fact = await tool(memory, "remember")(explicit, "明确请求记下的内容")

        auto = context(bot, memory, "他们聊得真热闹")
        auto.trigger_decision = SimpleNamespace(addressed=False, trigger_kind="automatic")

        # 写工具由宿主沙盒统一拒绝，回调不会执行，因此零落库。
        blocked, silent = await execute(bot, auto, "memory__remember", {"content": "自动参与不该保存"})
        assert blocked["error"] == "tool_blocked"
        assert blocked["reason"] == "automatic_side_effect"
        assert blocked["capability"] == "memory.write"
        assert blocked["risk"] == "write"
        assert silent is False
        blocked, _ = await execute(bot, auto, "memory__forget", {"fact_id": fact["id"]})
        assert blocked["error"] == "tool_blocked"
        assert blocked["reason"] == "automatic_side_effect"
        assert blocked["capability"] == "memory.write"
        assert [row["id"] for row in fact_rows(memory)] == [fact["id"]]

        # 只读回查在自动参与时仍可用，且只看当前用户自己的记录。
        recalled, _ = await execute(bot, auto, "memory__recall", {"query": "明确"})
        assert recalled["count"] == 1
        assert recalled["items"][0]["id"] == fact["id"]

        # 默认沙盒（allow_write=False）也放行明确 @ 的 memory.write 写操作。
        addressed = context(bot, memory, "记住我喜欢猫", at=SELF)
        addressed.trigger_decision = SimpleNamespace(addressed=True, trigger_kind="at")
        created, _ = await execute(bot, addressed, "memory__remember", {"content": "我喜欢猫"})
        assert created["content"] == "我喜欢猫"
        assert created["id"] != fact["id"]
        assert len(fact_rows(memory)) == 2
        assert bot.sandbox.stats.get("denied:automatic_side_effect", 0) == 2
    finally:
        await bot.stop()


async def test_disabled_plugin_and_group_setting_block_access(tmp_path):
    bot, memory = await setup_memory(tmp_path)
    try:
        remember, recall = tool(memory, "remember"), tool(memory, "recall")
        ctx = context(bot, memory)
        await remember(ctx, "先存一条")

        await bot.permissions.set_plugin_enabled("memory", False)
        silent = await dispatch(bot, "/记忆", message_id=1)
        assert silent.texts() == []
        denied, _ = await execute(bot, context(bot, memory), "memory__remember", {"content": "不该写入"})
        assert denied["error"] == "tool_permission_denied"
        assert len(fact_rows(memory)) == 1

        await bot.permissions.set_plugin_enabled("memory", True)
        await memory.settings.update({"enabled": False}, group_id=GROUP)
        closed = await dispatch(bot, "/记忆", message_id=2)
        assert "已关闭" in closed.last_text()
        disabled, _ = await execute(bot, context(bot, memory), "memory__remember", {"content": "也不该写入"})
        assert disabled["error"] == "disabled"
        assert (await recall(context(bot, memory)))["error"] == "disabled"
        assert len(fact_rows(memory)) == 1

        # 单群关闭不影响私聊。
        private_ctx = context(bot, memory, private=True)
        kept = await remember(private_ctx, "私聊仍然可以记")
        assert kept["id"] and (await recall(private_ctx))["count"] == 1
    finally:
        await bot.stop()


async def test_agent_instructions_follow_tool_visibility_and_auto_hint(tmp_path):
    bot, memory = await setup_memory(tmp_path)
    try:
        def sections(names):
            registry = {spec.qualified_name: (memory, spec) for spec in memory.ai_tools}
            available = [{"function": {"name": name}} for name in names]
            return instruction_sections({name: registry[name] for name in names}, available)

        all_names = [spec.qualified_name for spec in memory.ai_tools]
        sources = [item["source"] for item in sections(all_names)]
        assert sources == ["memory__memory_forget", "memory__memory_recall", "memory__memory_remember"]
        assert sections(all_names)[0]["text"]
        remember_text = next(
            item["text"] for item in sections(all_names) if item["source"].endswith("memory_remember")
        )
        assert "密码" in remember_text and "令牌" in remember_text and "隐私" in remember_text

        # 只授权一个工具时只注入对应片段。
        assert [item["source"] for item in sections(["memory__recall"])] == ["memory__memory_recall"]
        assert [item["source"] for item in sections(["memory__forget"])] == ["memory__memory_forget"]
        assert sections([]) == []

        await memory.settings.update({"auto_hint": False})
        assert sections(all_names) == []
        await memory.settings.update({"auto_hint": True})
        assert len(sections(all_names)) == 3
    finally:
        await bot.stop()
