from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import COMMANDS, BotService, TelegramHandlers
from bot.ports import NotReadyError

ALLOWED = 42


def make():
    coach = FakeCoach()
    return BotService(coach, FakeMemory(), FakeRouter(), ALLOWED), coach


class Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        self.sent.append((text, reply_markup))


def test_focus_command_returns_the_card_with_buttons():
    svc, coach = make()
    r = svc.focus()[0]
    assert "focus" in r.text and r.buttons
    assert coach.calls[-1] == ("focus",)


def test_focus_when_the_coach_is_not_ready():
    svc, coach = make()

    def boom():
        raise NotReadyError("coach.store indisponible")

    coach.focus = boom
    assert "Coach indisponible" in svc.focus()[0].text


def test_up_and_down_buttons_call_the_coach_and_return_the_new_card():
    svc, coach = make()
    up = svc.on_callback("fup:t1")
    assert coach.calls[-1] == ("adjust", "t1", True) and up.text == "ajusté t1"
    svc.on_callback("fdn:t1")
    assert coach.calls[-1] == ("adjust", "t1", False)


def test_focus_is_in_the_menu_and_the_help():
    svc, _ = make()
    assert "focus" in {n for n, _ in COMMANDS}
    assert "/focus" in svc.help()[0].text


def test_focus_handler_replies_with_markup():
    svc, _ = make()
    h = TelegramHandlers(svc)
    m = Msg()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ALLOWED), message=m, callback_query=None)
    asyncio.run(h.focus(update, SimpleNamespace(args=[])))
    assert len(m.sent) == 1 and m.sent[0][1] is not None


def test_focus_handler_ignores_other_users():
    svc, _ = make()
    h = TelegramHandlers(svc)
    m = Msg()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999), message=m, callback_query=None)
    asyncio.run(h.focus(update, SimpleNamespace(args=[])))
    assert m.sent == []


def test_nudge_buttons():
    svc, coach = make()
    go = svc.on_callback("nudgego:t1")
    assert coach.calls[-1] == ("start_now", "t1") and "Placé" in go.text
    no = svc.on_callback("nudgeno:t1")
    assert "plus" in no.text.lower() and coach.calls[-1] == ("start_now", "t1")  # rien de plus n'est appelé
