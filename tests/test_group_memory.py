"""Memory isolation, provenance and deletion use temporary databases only."""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.models import parse_event
from fllbot.plugin.context import Context
from fllbot.storage import MIGRATIONS, SCHEMA_VERSION, Storage
from fllbot.storage.memory import GroupMemoryStore
from helpers import FakeConnection, group_message, make_bot


@pytest.fixture
async def memory_store(tmp_path):
    storage = Storage(tmp_path / "memory.db")
    await storage.connect()
    try:
        yield storage, GroupMemoryStore(storage)
    finally:
        await storage.close()


async def test_scope_dedup_and_literal_search(memory_store):
    _, store = memory_store
    first = await store.append(1, 10, "user", "budget 100%_done", source_id=9, created_at=100)
    assert await store.append(1, 10, "user", "duplicate", source_id=9, created_at=100) == first
    await store.append(2, 10, "user", "another bot secret", created_at=100)
    await store.append(1, 20, "user", "another group secret", created_at=100)
    assert [r["id"] for r in await store.search(1, 10, "%_")] == [first]
    assert not await store.search(1, 10, "secret")
    assert not await store.search(1, 10, "")
    assert len(await store.recent(1, 10, 100)) == 1


async def test_summary_source_privacy_and_clear(memory_store):
    storage, store = memory_store
    source = await store.append(1, 10, "user", "original story", created_at=100)
    rows = await store.recent(1, 10, source + 1)
    await store.save_summary(1, 10, rows, "1111", [{"topic": "story"}])
    assert not await store.pending(1, 10, source + 1, "1111")
    assert len(await store.pending(1, 10, source + 1, "0000")) == 1
    assert len(await store.summaries(1, 10, "1111", 0, source + 1)) == 1
    assert not await store.summaries(1, 10, "0000", 0, source + 1)
    await storage.clear_chat("group", 10)
    # An in-flight compression cannot recreate a cleared memory.
    await store.save_summary(1, 10, rows, "1111", [{"topic": "story"}])
    assert not await store.recent(1, 10, source + 1)
    assert not await store.summaries(1, 10, "1111", 0, source + 1)


async def test_upgrade_preserves_existing_chat(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        for version in sorted(MIGRATIONS):
            if version <= 5:
                for statement in MIGRATIONS[version]:
                    connection.execute(statement)
        connection.execute("PRAGMA user_version=5")
        connection.execute(
            "INSERT INTO chat_messages(scene,scene_id,role,content,created_at) VALUES('group',10,'user','keep',100)"
        )
    storage = Storage(path)
    await storage.connect()
    try:
        assert (await storage.recent_chat("group", 10))[0].content == "keep"
        assert await storage.schema_version() == SCHEMA_VERSION
        store = GroupMemoryStore(storage)
        assert not await store.recent(1, 10, 100)
    finally:
        await storage.close()


@pytest.fixture
async def memory_bot(tmp_path, monkeypatch):
    bot = await make_bot(
        tmp_path,
        llm={"enabled": True, "api_key": "fake-test-key", "context_window": 100000},
        overrides={"chat": {"memory_recent_messages": 10}},
    )
    await bot.permissions.save_group(
        20002,
        enabled=True,
        config={
            "policy": {
                "auto_group": True,
                "trigger_modes": ["at"],
                "cooldown_seconds": 0,
            }
        },
    )
    topics = {
        "topics": [
            {
                "topic": "测试话题",
                "participants": "用户",
                "facts": "用户提问，尚无答复",
                "conclusions": "",
                "open_questions": "待回答",
                "status": "open",
            }
        ]
    }
    monkeypatch.setattr(
        bot.chat,
        "complete",
        AsyncMock(return_value=SimpleNamespace(text=json.dumps(topics, ensure_ascii=False), tool_calls=[])),
    )
    try:
        yield bot
    finally:
        await bot.stop()


def make_context(bot, text="现在聊什么", message_id=99, **kwargs):
    return Context(
        bot=bot, connection=FakeConnection(), event=parse_event(group_message(text, message_id=message_id, **kwargs))
    )


async def test_unanswered_background_is_seen_once_and_silent_input_is_kept(memory_bot):
    bot = memory_bot
    await bot.dispatcher.dispatch(parse_event(group_message("大家明天去公园", message_id=1)), FakeConnection())
    bot.chat.complete.assert_not_awaited()
    bot.chat.complete.return_value = SimpleNamespace(text="<NO_REPLY>", tool_calls=[])
    await bot.dispatcher.dispatch(parse_event(group_message("你觉得呢", at=10001, message_id=2)), FakeConnection())
    messages = bot.chat.complete.call_args.args[0]
    assert sum("大家明天去公园" in m["content"] for m in messages) == 1
    assert sum("你觉得呢" in m["content"] for m in messages) == 1
    assert len(await bot.chat.memory.store.recent(10001, 20002, 9999)) == 2


async def test_inactive_groups_and_disabled_memory_are_not_observed(memory_bot):
    bot = memory_bot
    await bot.chat.memory.observe(make_context(bot, group_id=90009))
    await bot.update_config({"chat": {"group_memory": False}})
    await bot.chat.memory.observe(make_context(bot))
    assert not await bot.chat.memory.store.recent(10001, 20002, 9999)
    assert not await bot.chat.memory.store.recent(10001, 90009, 9999)


async def test_old_topic_not_injected_and_recall_is_scoped(memory_bot):
    bot = memory_bot
    store = bot.chat.memory.store
    old = await store.append(10001, 20002, "user", "昨天故事里的小猫", created_at=time.time() - 86400)
    foreign = await store.append(10002, 20002, "user", "别的机器人秘密", created_at=time.time() - 86400)
    ctx = make_context(bot)
    await bot.chat.memory.observe(ctx)
    messages = await bot.chat.build_messages("group", 20002, ctx.text, ctx=ctx)
    assert all("昨天故事里的小猫" not in m["content"] for m in messages)
    assert "新一段对话" in messages[0]["content"]
    assert "error" in await bot.chat.memory.search(ctx, "小猫")
    recall = make_context(bot, "昨天那个故事继续", message_id=100)
    await bot.chat.memory.observe(recall)
    result = await bot.chat.memory.search(recall, "小猫")
    assert [r["message_id"] for r in result["messages"]] == [old]
    assert not (await bot.chat.memory.search(recall, message_id=foreign))["messages"]
    assert "error" in await bot.chat.memory.search(recall, start_date="not-a-date")


async def test_compression_uses_originals_once_and_keeps_provenance(memory_bot):
    bot = memory_bot
    store = bot.chat.memory.store
    for i in range(14):
        await store.append(10001, 20002, "user", f"原文{i}", created_at=time.time() - 100 + i)
    ctx = make_context(bot)
    await bot.chat.memory.observe(ctx)
    messages = await bot.chat.build_messages("group", 20002, ctx.text, ctx=ctx)
    bot.chat.complete.assert_not_awaited()
    bot.chat.memory.schedule(ctx)
    await bot.chat.memory._summary_queue.join()
    messages = await bot.chat.build_messages("group", 20002, ctx.text, ctx=ctx)
    bot.chat.complete.assert_awaited_once()
    summary_input = bot.chat.complete.call_args.args[0][-1]["content"]
    assert "原文0" in summary_input and "原文3" in summary_input and "原文4" not in summary_input
    assert any("来源消息" in m["content"] and "测试话题" in m["content"] for m in messages)
    assert len(await store.recent(10001, 20002, 9999)) == 15
    bot.chat.memory._retry_after.clear()
    await bot.chat.build_messages("group", 20002, ctx.text, ctx=ctx)
    bot.chat.complete.assert_awaited_once()


async def test_recent_history_is_count_based_even_beyond_an_hour(memory_bot):
    bot = memory_bot
    now = time.time()
    # Fifteen messages across 2.5 hours, with no gap large enough to start a new conversation.
    for i in range(15):
        await bot.chat.memory.store.append(10001, 20002, "user", f"连续消息{i}", created_at=now - (15 - i) * 600)
    ctx = make_context(bot)
    await bot.chat.memory.observe(ctx)
    messages = await bot.chat.build_messages("group", 20002, ctx.text, ctx=ctx)
    original = [m["content"] for m in messages[1:-1] if "较早话题摘要" not in m["content"]]
    assert len(original) == 10
    assert "连续消息5" in original[0]  # 100 minutes old, still within the most recent 10 messages.
    assert "连续消息14" in original[-1]
    bot.chat.memory.schedule(ctx)
    await bot.chat.memory._summary_queue.join()
    source = bot.chat.complete.call_args.args[0][-1]["content"]
    assert "连续消息4" in source and "连续消息5" not in source


async def test_failed_summary_keeps_raw_and_privacy_flags_apply(memory_bot):
    bot = memory_bot
    store = bot.chat.memory.store
    for i in range(12):
        await store.append(
            10001,
            20002,
            "user",
            f"内容{i}",
            created_at=time.time() - 100 + i,
            user_id=123456,
            nickname="隐私昵称",
            sender_role="owner",
        )
    await bot.update_config(
        {"chat": {"send_nickname": False, "send_user_id": False, "send_role": False, "send_time": False}}
    )
    bot.chat.complete.return_value = SimpleNamespace(text="invalid json", tool_calls=[])
    ctx = make_context(bot, "之前的内容", message_id=100)
    await bot.chat.memory.observe(ctx)
    messages = await bot.chat.build_messages("group", 20002, ctx.text, ctx=ctx)
    assert "隐私昵称" not in json.dumps(messages, ensure_ascii=False)
    bot.chat.memory.schedule(ctx)
    await bot.chat.memory._summary_queue.join()
    assert "123456" not in bot.chat.complete.call_args.args[0][-1]["content"]
    assert len(await store.recent(10001, 20002, 9999)) == 13
    excerpts = await store.summaries(10001, 20002, "0000", 0, 9999)
    assert excerpts and 'source_excerpt' in excerpts[0]['content']
    assert '123456' not in excerpts[0]['content'] and '隐私昵称' not in excerpts[0]['content']
    assert (await bot.chat.memory.search(ctx, "内容0"))["messages"]


async def test_large_messages_respect_working_and_summary_budgets(memory_bot):
    bot = memory_bot
    await bot.update_config({"llm": {"context_window": 8192}, "chat": {"memory_context_chars": 4000}})
    store = bot.chat.memory.store
    old = await store.append(10001, 20002, "user", "旧长文" * 10000, created_at=time.time() - 7200)
    await store.append(10001, 20002, "user", "新长文" * 10000, created_at=time.time() - 10)
    ctx = make_context(bot, "之前的长文继续")
    await bot.chat.memory.observe(ctx)
    messages = await bot.chat.build_messages("group", 20002, ctx.text, ctx=ctx)
    assert sum(len(m["content"]) for m in messages) <= 4000
    bot.chat.memory.schedule(ctx)
    await bot.chat.memory._summary_queue.join()
    source = bot.chat.complete.call_args.args[0][-1]["content"]
    assert len(source.encode("utf-8")) <= 8192 - 3248
    assert "中间内容省略" in source
    recalled = await bot.chat.memory.search(ctx, message_id=old)
    first = recalled["messages"][0]
    assert first["next_offset"]
    second = await bot.chat.memory.search(ctx, message_id=old, offset=first["next_offset"])
    assert second["messages"][0]["next_offset"] > first["next_offset"]


async def test_cooldown_does_not_drop_background_memory(memory_bot):
    bot = memory_bot
    await bot.permissions.save_group(
        20002,
        enabled=True,
        config={
            "policy": {
                "auto_group": True,
                "trigger_modes": ["all"],
                "cooldown_seconds": 120,
            }
        },
    )
    bot.chat.complete.return_value = SimpleNamespace(text="收到", tool_calls=[])
    connection = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message("第一句话", message_id=1)), connection)
    await bot.dispatcher.dispatch(parse_event(group_message("第二句话", message_id=2)), connection)
    assert bot.chat.complete.await_count == 1
    raw = await bot.chat.memory.store.recent(10001, 20002, 9999)
    assert [r["content"] for r in raw] == ["第一句话", "收到", "第二句话"]


async def test_history_tool_is_gated_and_returns_the_following_reply(memory_bot):
    bot = memory_bot
    store = bot.chat.memory.store
    await store.append(10001, 20002, "user", "昨天讲个故事", created_at=time.time() - 100)
    answer = await store.append(10001, 20002, "assistant", "有一只猫住在书店里", created_at=time.time() - 90)
    calls = []

    async def complete(messages, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) == 1:
            assert any(t["function"]["name"] == "agent__search_history" for t in kwargs["tools"])
            return SimpleNamespace(
                text="",
                tool_calls=[
                    {
                        "id": "recall",
                        "type": "function",
                        "function": {
                            "name": "agent__search_history",
                            "arguments": '{"query":"故事"}',
                        },
                    }
                ],
            )
        result = json.loads(messages[-1]["content"])
        assert result["messages"][0]["next_message_id"] == answer
        return SimpleNamespace(text="我找到昨天的故事记录了", tool_calls=[])

    bot.chat.complete.side_effect = complete
    connection = FakeConnection()
    await bot.dispatcher.dispatch(parse_event(group_message("昨天的故事还记得吗", at=10001, message_id=10)), connection)
    assert connection.texts() == ["我找到昨天的故事记录了"]
    bot.chat.complete.side_effect = None
    bot.chat.complete.return_value = SimpleNamespace(text="你好", tool_calls=[])
    await bot.dispatcher.dispatch(parse_event(group_message("你好", at=10001, message_id=11)), connection)
    assert not any(
        t["function"]["name"] == "agent__search_history" for t in (bot.chat.complete.call_args.kwargs["tools"] or [])
    )

async def test_fenced_summary_is_saved(memory_bot):
    bot = memory_bot
    original = bot.chat.complete.return_value.text
    bot.chat.complete.return_value = SimpleNamespace(text=f"```json\n{original}\n```", tool_calls=[])
    store = bot.chat.memory.store
    await store.append(10001, 20002, "user", "话题原文", created_at=time.time() - 100)
    ctx = make_context(bot)
    await bot.chat.memory.compress(ctx, 9999)
    assert len(await store.summaries(10001, 20002, bot.chat.memory.privacy(), 0, 9999)) == 1
    assert len(await store.recent(10001, 20002, 9999)) == 1


@pytest.mark.parametrize('text', ['{"topics":[]}', '{"topics":null}', 'not json',
    '{"topics":[{"topic":"test","facts":"source","status":[]}]}'])
async def test_invalid_summary_uses_explicit_excerpts_and_does_not_block_batch(memory_bot,text,caplog):
    bot = memory_bot
    store = bot.chat.memory.store
    await store.append(10001,20002,'user','原始记录不会丢失',created_at=time.time()-100)
    bot.chat.complete.return_value = SimpleNamespace(text=text,tool_calls=[])
    context = make_context(bot)
    await bot.chat.memory.compress(context,9999)
    summaries = await store.summaries(10001,20002,bot.chat.memory.privacy(),0,9999)
    topics = json.loads(summaries[0]['content'])
    assert topics[0]['summary_kind'] == 'source_excerpt'
    assert topics[0]['source_incomplete']
    assert '原始记录不会丢失' in topics[0]['facts']
    assert len(await store.recent(10001,20002,9999)) == 1
    bot.chat.memory._retry_after.clear()
    await bot.chat.memory.compress(context,9999)
    bot.chat.complete.assert_awaited_once()
    assert not any(record.exc_info for record in caplog.records)


async def test_summary_network_failure_backs_off_without_advancing_sources(memory_bot):
    bot = memory_bot
    await bot.chat.memory.store.append(10001,20002,'user','待压缩原文',created_at=time.time()-100)
    bot.chat.complete.side_effect = RuntimeError('network unavailable')
    context = make_context(bot)
    for expected in (60,120,240):
        bot.chat.memory._retry_after.clear()
        before = time.time()
        await bot.chat.memory.compress(context,9999)
        assert bot.chat.memory._retry_after[(10001,20002)] >= before + expected
        count = bot.chat.complete.await_count
        await bot.chat.memory.compress(context,9999)
        assert bot.chat.complete.await_count == count
    assert len(await bot.chat.memory.store.pending(10001,20002,9999,bot.chat.memory.privacy())) == 1


def test_summary_compatible_shapes_and_string_arrays():
    from fllbot.llm.summary_protocol import parse_summary

    topic = {'topic':'原话','participants':['甲','乙'],'facts':['确有提问','尚未答复'],
             'conclusions':None,'open_questions':None,'status':'open'}
    for document in ([topic],{'topics':topic},{'topics':[topic]}):
        result = parse_summary(json.dumps(document))
        assert result[0]['participants'] == '甲；乙'
        assert result[0]['facts'] == '确有提问；尚未答复'
        assert result[0]['conclusions'] == ''


async def test_slow_summary_is_deferred_and_queue_is_bounded(memory_bot, monkeypatch):
    bot = memory_bot
    for i in range(14):
        await bot.chat.memory.store.append(10001, 20002, 'user', str(i), created_at=time.time()-100+i)
    ctx = make_context(bot)
    await bot.chat.memory.observe(ctx)
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(*args):
        entered.set()
        await release.wait()
    monkeypatch.setattr(bot.chat.memory, 'compress', slow)
    await asyncio.wait_for(bot.chat.build_messages('group', 20002, ctx.text, ctx=ctx), 1)
    assert not entered.is_set()
    bot.chat.memory.schedule(ctx)
    await asyncio.wait_for(entered.wait(), 1)
    bot.chat.memory.schedule(ctx)
    assert bot.chat.memory._summary_queue.qsize() == 0
    # Other groups use bounded queue slots, never spawn one task per message.
    for group in range(30000, 30100):
        await bot.permissions.save_group(group, enabled=True, config={'policy':{'auto_group':True}})
        pending = make_context(bot, group_id=group)
        pending.memory_summary_before = 100
        bot.chat.memory.schedule(pending)
    assert bot.chat.memory._summary_queue.qsize() == 64
    assert bot.chat.memory.summary_dropped == 36
    release.set()
    await asyncio.wait_for(bot.chat.memory._summary_queue.join(), 1)


async def test_summary_generation_cannot_commit_after_epoch_change(memory_store):
    storage, store = memory_store
    source = await store.append(1, 10, 'user', 'source', created_at=100)
    rows = await store.recent(1, 10, source+1)
    epoch = storage.conversation_generation
    await storage.clear_chat('group', 20)
    await store.save_summary(1, 10, rows, '1111', [{'topic':'stale'}], generation=epoch)
    assert not await store.summaries(1, 10, '1111', 0, source+1)
    assert await store.recent(1, 10, source+1)


@pytest.mark.parametrize("provider_timeout, expected", [(60, 120), (240, 270)])
async def test_summary_deadline_allows_slow_provider(memory_bot, monkeypatch, provider_timeout, expected):
    from fllbot.llm import memory as memory_module

    bot = memory_bot
    await bot.chat.memory.store.append(10001, 20002, "user", "待整理消息", created_at=time.time() - 100)
    resolve = bot.chat.client.resolve

    def configured_provider(**kwargs):
        name, config = resolve(**kwargs)
        return name, config.model_copy(update={"timeout": provider_timeout})

    deadlines = []

    async def wait_for(awaitable, *, timeout):
        deadlines.append(timeout)
        return await awaitable

    monkeypatch.setattr(bot.chat.client, "resolve", configured_provider)
    monkeypatch.setattr(memory_module, "asyncio", SimpleNamespace(wait_for=wait_for))
    await bot.chat.memory.compress(make_context(bot), 9999)
    assert deadlines == [expected]
    assert len(await bot.chat.memory.store.summaries(10001, 20002, bot.chat.memory.privacy(), 0, 9999)) == 1


async def test_summary_timeout_retries_smaller_batch_and_keeps_originals(memory_bot, caplog):
    bot = memory_bot
    memory = bot.chat.memory
    for i in range(4):
        await memory.store.append(10001, 20002, "user", str(i) + "原文" * 1300, created_at=time.time() - 100 + i)
    context = make_context(bot)
    valid_response = bot.chat.complete.return_value
    bot.chat.complete.side_effect = TimeoutError()
    await memory.compress(context, 9999)
    original_sources = json.loads(bot.chat.complete.call_args.args[0][-1]["content"])
    assert "请求超过 120 秒" in caplog.text
    assert len(await memory.store.pending(10001, 20002, 9999, memory.privacy())) == 4
    memory._retry_after.clear()
    bot.chat.complete.side_effect = None
    bot.chat.complete.return_value = valid_response
    await memory.compress(context, 9999)
    retry_sources = json.loads(bot.chat.complete.call_args.args[0][-1]["content"])
    assert 0 < len(retry_sources) < len(original_sources)
    assert (10001, 20002) not in memory._summary_failures
    assert len(await memory.store.recent(10001, 20002, 9999)) == 4
    assert len(await memory.store.pending(10001, 20002, 9999, memory.privacy())) == 4 - len(retry_sources)
