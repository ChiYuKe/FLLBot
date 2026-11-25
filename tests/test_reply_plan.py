from dataclasses import FrozenInstanceError

import pytest

from fllbot.builtin_plugins.chat_dispatch.plugin import ChatDispatch
from fllbot.config import ChatConfig
from fllbot.llm.reply import ReplyService


def test_reply_plan_freezes_formatting_and_returns_independent_segments():
    options = ChatConfig(reply_mode='split',reply_chunk_size=50,quote_reply=True,at_sender=True)
    plan = ReplyService().build('x'*100,turn_id='turn',self_id=11,scene='group',scene_id=123,
        message_id=987,user_id=456,options=options,default_chunk_size=50)
    options.quote_reply = False
    assert [len(part.text) for part in plan.parts] == [50,50]
    assert plan.parts[0].quote_id == 987
    assert plan.parts[0].at_user_id == 456
    generated = plan.formatting()
    generated['part_segments']['0'].clear()
    assert len(plan.formatting()['part_segments']['0']) == 4
    with pytest.raises(FrozenInstanceError):
        plan.mode = 'forward'


@pytest.mark.parametrize("text, mode", [
    ("x" * 50, "split"),
    ("x" * 95, "split"),
    ("```text\n" + "x" * 80 + "\n```", "forward"),
    ("x" * 140, "forward"),
])
def test_fragmented_replies_can_be_combined_into_one_forward(text, mode):
    plan = ReplyService().build(
        text, turn_id="turn", self_id=11, scene="group", scene_id=123, message_id=987, user_id=456,
        options=ChatConfig(reply_mode="split", reply_chunk_size=50), default_chunk_size=50,
        preserve_whitespace=True, forward_if_fragmented=ChatDispatch().should_forward,
    )
    assert plan.mode == mode
    assert "".join(part.text for part in plan.parts) == text
