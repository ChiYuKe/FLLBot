"""Daily plugin persistence, authorization and delivery tests; no network."""

import importlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fllbot.onebot.segments import MessageSegment
from helpers import make_bot

daily = importlib.import_module("plugins.daily.plugin")


def context(tmp_path, *, bot=100, group=200, user=300, admin=False):
    return SimpleNamespace(
        data_dir=tmp_path,
        self_id=bot,
        group_id=group,
        user_id=user,
        is_group=group is not None,
        is_group_admin=admin,
        message=[],
        connection=SimpleNamespace(call=AsyncMock()),
        get_members=AsyncMock(return_value=[]),
    )


async def test_tasks_persist_and_isolate_owner_scene_and_account(tmp_path):
    ctx = context(tmp_path)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    item = await daily.create_task(ctx, "交作业", future)
    assert item["id"]
    assert (await daily.list_tasks(ctx))["items"][0]["content"] == "交作业"
    for other in (context(tmp_path, bot=101), context(tmp_path, group=201), context(tmp_path, user=301)):
        assert not (await daily.list_tasks(other))["items"]
        assert not (await daily.update_task(other, item["id"], "cancel"))["updated"]
    assert (await daily.update_task(ctx, item["id"], "complete"))["updated"]
    assert not (await daily.list_tasks(ctx))["items"]
    assert (await daily.list_tasks(ctx, True))["items"][0]["state"] == "completed"
    assert "error" in await daily.create_task(ctx, "过去", "2000-01-01T00:00:00")
    assert "error" in await daily.create_task(ctx, " ")


async def test_knowledge_permissions_and_isolation(tmp_path):
    member = context(tmp_path)
    admin = context(tmp_path, admin=True)
    assert "error" in await daily.save_knowledge(member, "群规", "正文")
    assert (await daily.save_knowledge(admin, "群规", "正文"))["saved"]
    assert (await daily.save_knowledge(admin, "群规", "更新正文"))["saved"]
    assert (await daily.search_knowledge(member, "更新"))["items"][0]["content"] == "更新正文"
    for other in (context(tmp_path, bot=101), context(tmp_path, group=201)):
        assert not (await daily.search_knowledge(other))["items"]
    assert "error" in await daily.delete_knowledge(member, "群规")
    assert (await daily.delete_knowledge(admin, "群规"))["deleted"]


async def test_reply_only_reads_direct_reference_in_current_scene(tmp_path):
    ctx = context(tmp_path)
    assert "error" in await daily.read_reply(ctx)
    ctx.connection.call.assert_not_awaited()
    ctx.message = [MessageSegment.reply(123)]
    ctx.connection.call.return_value = {"group_id": 999, "message": "秘密"}
    assert "error" in await daily.read_reply(ctx)
    ctx.connection.call.return_value = {"group_id": 200, "message": "原消息"}
    assert (await daily.read_reply(ctx))["content"] == "原消息"
    ctx.connection.call.return_value = {"message": "没有群号"}
    assert "error" in await daily.read_reply(ctx)


async def test_members_filters_without_exposing_extra_fields(tmp_path):
    ctx = context(tmp_path)
    ctx.get_members.return_value = [
        dict(user_id=1, nickname="小明", role="admin", age=18),
        dict(user_id=2, nickname="小红"),
    ]
    result = await daily.find_members(ctx, "小明")
    assert result["total"] == 1
    assert "age" not in result["items"][0]
    assert "error" in await daily.find_members(context(tmp_path, group=None))


async def test_reminders_reconnect_retry_cancel_and_switches(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    task = await daily.create_task(ctx, "提醒我")
    cancelled = await daily.create_task(ctx, "取消我")
    await daily.update_task(ctx, cancelled["id"], "cancel")
    with daily.database(tmp_path) as db:
        db.execute("UPDATE tasks SET due=1")
    connection = SimpleNamespace(send_msg=AsyncMock())
    permissions = SimpleNamespace(
        plugin_enabled=lambda *args: True, group_enabled=lambda *args: True, allow_private=True
    )
    bot = SimpleNamespace(root=tmp_path, manager=SimpleNamespace(by_self_id=lambda _: None), permissions=permissions)
    monkeypatch.setattr(daily.plugin, "bot", bot)
    # Match the real plugin's runtime data directory.
    monkeypatch.setattr(type(daily.plugin), "data_dir", property(lambda _: tmp_path))
    await daily.deliver_reminders(bot)
    connection.send_msg.assert_not_awaited()
    bot.manager.by_self_id = lambda _: connection
    with daily.database(tmp_path) as db:
        db.execute("UPDATE tasks SET retry_at=0")
    permissions.group_enabled = lambda _: False
    await daily.deliver_reminders(bot)
    connection.send_msg.assert_not_awaited()
    with daily.database(tmp_path) as db:
        db.execute("UPDATE tasks SET retry_at=0")
    permissions.group_enabled = lambda _: True
    connection.send_msg.side_effect = RuntimeError("offline")
    await daily.deliver_reminders(bot)
    with daily.database(tmp_path) as db:
        assert db.execute("SELECT notified FROM tasks WHERE id=?", (task["id"],)).fetchone()[0] == 0
        db.execute("UPDATE tasks SET retry_at=0")
    connection.send_msg.side_effect = None
    await daily.deliver_reminders(bot)
    assert connection.send_msg.await_count == 1  # Missing receipt never automatically retries.
    current = (await daily.task_console('list'))['items']
    current = next(row for row in current if row['id'] == task['id'])
    assert current['delivery'] == 'uncertain'
    await daily.task_console('action', task_id=task['id'], action='retry', revision=current['revision'], acknowledge_duplicate=True)
    await daily.deliver_reminders(bot)
    await daily.deliver_reminders(bot)
    assert connection.send_msg.await_count == 2  # failed attempt + one successful send
    args = connection.send_msg.call_args
    assert args.kwargs["group_id"] == 200
    assert args.args[0][0].data["qq"] == "300"


def test_time_defaults_to_beijing():
    stamp = daily.due_time("2099-01-01T08:00:00")
    assert datetime.fromtimestamp(stamp, UTC).hour == 0


def test_tools_have_explicit_schemas():
    assert len(daily.plugin.ai_tools) == 8
    for tool in daily.plugin.ai_tools:
        assert tool.parameters["additionalProperties"] is False


async def test_real_loader_reload_preserves_data(tmp_path):
    root = tmp_path / "plugins"
    shutil.copytree(Path(__file__).resolve().parents[1] / "plugins/daily", root / "daily")
    bot = await make_bot(tmp_path, builtin=False, plugin_dirs=[root])
    try:
        plugin = bot.plugins.get("daily")
        assert plugin is not None
        assert len(plugin.ai_tools) == 8
        ctx = context(plugin.data_dir)
        create = next(tool.callback for tool in plugin.ai_tools if tool.name == "create_task")
        await create(ctx, "重载后保留")
        await bot.plugins.reload("daily")
        plugin = bot.plugins.get("daily")
        read = next(tool.callback for tool in plugin.ai_tools if tool.name == "list_tasks")
        assert (await read(ctx))["items"][0]["content"] == "重载后保留"
        assert not bot.plugins.failed
    finally:
        await bot.stop()
