from __future__ import annotations

from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import BotService


def test_go_callback_reaches_the_assistant():
    coach = FakeCoach()
    svc = BotService(coach, FakeMemory(), FakeRouter(), 42)
    r = svc.on_callback("ps:go:abcd1234")
    assert coach.calls[-1] == ("assistant_action", "ps", "go:abcd1234") and r.text == "assistant ps"
