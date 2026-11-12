from types import SimpleNamespace
from unittest.mock import AsyncMock

from fllbot.onebot.models import parse_event
from fllbot.participation import ParticipationSignals, evaluate
from helpers import FakeConnection, group_message, make_bot


def test_scores_are_explainable_and_additive():
    decision = evaluate(ParticipationSignals(text="哈哈哈哈"), -10)
    assert not decision.participate
    assert dict(decision.factors)["laughter_only"] == -30
    assert decision.summary()["score"] == -30

    question = evaluate(ParticipationSignals(text="今天谁值班？"), -10)
    assert question.participate
    assert dict(question.factors)["question"] == 15

    addressed = evaluate(ParticipationSignals(text="嗯", addressed=True), 50)
    assert addressed.participate

    repeated = evaluate(ParticipationSignals(text="真的？", repeated=True), 0)
    assert repeated.score == 15 - 20
    assert not repeated.participate

    dominant = evaluate(ParticipationSignals(text="嗯", bot_replies_recent=5), -10)
    assert not dominant.participate

    disabled = evaluate(ParticipationSignals(text="哈哈哈"), -100)
    assert disabled.participate


async def test_participation_threshold_filters_model_calls(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake", "context_window": 8192})
    try:
        agent = bot.plugins.get("agent")
        await agent.settings.update({
            "auto_group": True, "trigger_mode": "all", "cooldown_seconds": 0,
            "participation_threshold": -10,
        })
        complete = AsyncMock(return_value=SimpleNamespace(text="收到", tool_calls=[]))
        monkeypatch.setattr(bot.chat, "complete", complete)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(group_message("哈哈哈哈", message_id=1)), connection)
        assert complete.await_count == 0
        assert not connection.texts()

        await bot.dispatcher.dispatch(parse_event(group_message("你好", message_id=2)), connection)
        assert complete.await_count == 1
        decisions = await bot.storage.list_trigger_decisions(self_id=10001)
        reasons = [item["reason"] for item in decisions["items"]]
        assert "participation_below_threshold" in reasons
    finally:
        await bot.stop()
