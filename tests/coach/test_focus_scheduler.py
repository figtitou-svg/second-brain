from __future__ import annotations

import json
import re

import pytest

from coach import store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 15, 9, 0)  # jeudi


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    db = Database(":memory:")
    cal = FakeCalendarClient([(dt(2026, 10, 15, 18, 30), dt(2026, 10, 15, 21, 30))])
    sent: list[tuple[str, list | None]] = []
    clock = Clock(NOW)
    s = CoachScheduler(db, cal, load_rules(FROZEN), lambda text, buttons=None: sent.append((text, buttons)), clock)
    return s, db, cal, sent, clock


def mk(title, minutes=30, due=None, **kw):
    return Task(title=title, estimated_minutes=minutes, due=due or dt(2026, 10, 20, 18), **kw)


class FakeLLM:
    def __init__(self):
        self.calls = []

    def __call__(self, prompt, *, sensitivity, system=None, json_mode=False):
        self.calls.append(prompt)
        return json.dumps({i: "Phrase naturelle." for i in re.findall(r"id=(\S+)", prompt)})


def test_briefing_has_the_focus_block_under_the_title(env):
    s, db, *_ = env
    db.upsert_task(mk("Rendu IA", due=dt(2026, 10, 16, 18)))
    text = s.briefing_text()
    assert text.startswith("☀️")
    assert text.index("Ton focus") < text.index("Échéances sous 48 h")
    assert "Rendu IA" in text.split("Ton focus")[1]


def test_briefing_without_tasks_says_free_day(env):
    s, db, cal, *_ = env
    cal.external_busy = []  # la fixture a un créneau occupé : sans lui, le programme est vide
    text = s.briefing_text()
    assert "journée libre" in text
    assert "Ton focus" not in text and "Rien de prévu" not in text


def test_briefing_with_tasks_and_empty_agenda_has_no_nothing_planned(env):
    s, db, cal, *_ = env
    cal.external_busy = []
    db.upsert_task(mk("Rendu IA", due=dt(2026, 10, 25, 18)))
    text = s.briefing_text()
    assert "Ton focus" in text and "Rien de prévu" not in text


def test_briefing_uses_the_llm_phrase(env):
    s, db, *_ = env
    s.explain_fn = FakeLLM()
    db.upsert_task(mk("Rendu IA", due=dt(2026, 10, 16, 18)))
    assert "Phrase naturelle." in s.briefing_text()


def test_briefing_message_carries_the_focus_buttons(env):
    s, db, cal, sent, clock = env
    db.upsert_task(mk("Rendu IA", due=dt(2026, 10, 16, 18)))
    s.tick(dt(2026, 10, 15, 7, 50))
    text, buttons = next((m, b) for m, b in sent if "Programme" in m)
    assert buttons and buttons[0][0][1].startswith("td:") and buttons[0][1][1].startswith("fup:")


def test_focus_card_now_mode_shows_time_before_next_event(env):
    s, db, cal, *_ = env
    cal.external_busy = [(dt(2026, 10, 15, 10, 30), dt(2026, 10, 15, 12, 0))]
    db.upsert_task(mk("Rendu", due=dt(2026, 10, 16, 18)))
    text, buttons = s.focus_card(now_mode=True)
    assert "À faire maintenant" in text and "Tu as 1 h 30 avant ton prochain événement" in text
    assert len(buttons) == 1


def test_adjust_up_and_down_update_boost_and_weight(env):
    s, db, *_ = env
    t = mk("[Alg] TP")
    db.upsert_task(t)
    text, buttons = s.adjust_importance(t.id, True)
    assert db.get_task(t.id).boost == 1 and db.get_weights() == {"Alg": 1.1}
    assert "Ton focus" in text and buttons
    s.adjust_importance(t.id, False)
    s.adjust_importance(t.id, False)
    assert db.get_task(t.id).boost == -1
    assert db.get_weights()["Alg"] == pytest.approx(0.891)


def test_adjust_on_a_finished_task_changes_nothing(env):
    s, db, *_ = env
    t = mk("[Alg] TP", status="done")
    db.upsert_task(t)
    text, _ = s.adjust_importance(t.id, True)
    assert "déjà traitée" in text
    assert db.get_task(t.id).boost == 0 and db.get_weights() == {}


def test_store_facade(env):
    s, db, *_ = env
    store.configure(db, s)
    t = mk("Rendu", due=dt(2026, 10, 16, 18))
    db.upsert_task(t)
    text, buttons = store.focus()
    assert "À faire maintenant" in text and buttons
    store.adjust_importance(t.id, True)
    assert db.get_task(t.id).boost == 1
