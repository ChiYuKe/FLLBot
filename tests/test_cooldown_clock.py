"""冷却实现必须在刚启动、monotonic() 很小的机器上也正确。"""

from __future__ import annotations

import pytest

from helpers import make_bot


@pytest.mark.parametrize("uptime", [0.5, 1.0, 30.0])
async def test_first_cooldown_call_is_allowed_on_fresh_boot(tmp_path, monkeypatch, uptime):
    bot = await make_bot(tmp_path)
    try:
        # 模拟刚开机的 CI/嵌入式环境：monotonic() 远小于冷却窗口（600s）。
        monkeypatch.setattr("fllbot.permission.time.monotonic", lambda: uptime)
        key = ("agent", "natural_language:10001:20002", 30003)
        assert bot.permissions.cooldown_ok(key, 600) is True
        # 同一 key 的第二次调用仍在窗口内；0.0 哨兵会让首次调用就失败。
        assert bot.permissions.cooldown_ok(key, 600) is False
        assert bot.permissions.cooldown_remaining(key, 600) == pytest.approx(600 - 0.0)
    finally:
        await bot.stop()


async def test_cooldown_recovers_after_monotonic_uptime(tmp_path, monkeypatch):
    bot = await make_bot(tmp_path)
    try:
        clock = {"now": 1.0}
        monkeypatch.setattr("fllbot.permission.time.monotonic", lambda: clock["now"])
        key = ("plugin", "handler", 42)
        assert bot.permissions.cooldown_ok(key, 60) is True
        clock["now"] = 30.0
        assert bot.permissions.cooldown_ok(key, 60) is False
        assert bot.permissions.cooldown_remaining(key, 60) == pytest.approx(31.0)
        clock["now"] = 61.0
        assert bot.permissions.cooldown_ok(key, 60) is True
        # 成功调用会刷新计时，因此剩余时间回到完整窗口。
        assert bot.permissions.cooldown_remaining(key, 60) == pytest.approx(60.0)
    finally:
        await bot.stop()
